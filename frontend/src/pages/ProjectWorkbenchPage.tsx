import React, { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { t } from '@/i18n';
import { projectsApi, keyframesApi, storyboardApi, videoApi, qcApi, autopilotApi, upscaleApi, chatApi, agentApi, episodesApi, novelsSplitPlanApi, preflightApi, screenplayApi, characterOutfits, characterSheetUpload, assetPrecipitation, generationApi, type VideoMode, type EpisodeScenesResponse, type ChapterPreflightResult, type AssetPrecipitationResponse, type PrecipitationStatus } from '@/api/client';
import { Button, ConfirmDialog, Input, EmptyState, ErrorState, Loading, Skeleton, Modal, Select } from '@/components/ui';
// tab 图标統一走线性 SVG（方案 P2-10）：此前是 emoji，字号受系统字体影响且观感与全站割裂
import {
  AlertTriangle, BarChart3, Box, Check, CheckCircle2, Clapperboard, ClipboardCheck, ClipboardList, FileText,
  FolderOpen, ImageIcon, MessageSquare, Mountain, Music, Pause, Play, Share2, Upload, User, X, ZoomIn,
} from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import { useComfyProgress } from '@/hooks/useComfyProgress';
import { getAgentSession, type ChatMsg } from '@/agentSession';
import { OutputReviewTab } from '@/components/OutputReviewTab';
import { AudioTab } from '@/components/AudioTab';
import type { Project, Deliverable, UpscaleEnv, UpscaleSource, UpscaleTask, UpscaleArtifact, AgentStep, AutopilotCurrent, CharacterOutfit, ShotGridStatusResponse, ShotGridTaskState } from '@/types';

// 焦点环：与 components/ui/index.tsx 里的 FOCUS_RING 逐字一致。
// index.css 有全局 :focus-visible outline 兜底，这里显式加 focus:outline-none 把它压掉，
// 否则 outline + ring 会叠成双环。凡因形状/类型原因换不成共享组件的原生控件，统一补这一串。
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

// ========== Workbench Tab Types ==========
// 注意：'chat' 已移除 —— AI 总控改成了右侧常驻面板，不再是标签页（见 ChatPanel）
import AutonomousPanel from '@/components/AutonomousPanel';
import QualityPanel from '@/components/QualityPanel';
import TriagePanel from '@/components/TriagePanel';
import ExportPanel from '@/components/ExportPanel';

type WorkbenchTab = 'overview' | 'storyboard' | 'qc' | 'upscale' | 'audio' | 'output';

interface AssetItem {
  name: string;
  url?: string;
  file?: string;
  size?: number;
  /** 文件 mtime（后端下发的版本令牌）：拼进图片 URL 做缓存击穿，保证重生成后前端必刷新 */
  mtime?: number;
  [key: string]: any;
}

interface ProjectAssets {
  success: boolean;
  project: string;
  gallery: Record<string, AssetItem[]>;
  storyboards: AssetItem[];
  videos: AssetItem[];
  final: AssetItem[];
  dub: AssetItem[];
  counts: Record<string, number>;
}

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
              <StoryboardHubTab projectKey={projectKey} novelId={project.novel_id} />
            )}
            {activeTab === 'qc' && (
              <>
                <QcTab projectKey={projectKey} />

                {/* 质量：原文覆盖率 / 字幕校对 / 跨集一致性 / 项目连贯性 */}
                <QualityPanel project={projectKey} />

                {/* 诊断与补救建议（POST /api/qc/triage）—— 只给建议，点了才执行 */}
                <TriagePanel project={projectKey} />
              </>
            )}
            {activeTab === 'audio' && (
              <>
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

// ========== 资源地址解析 ==========
// 后端 /api/projects/<pid>/assets 返回的 url 已带 "/api" 前缀，不能重复拼接
function assetSrc(url?: string | null, version?: number | string | null): string | null {
  if (!url) return null;
  let out = url;
  if (!(out.startsWith('http://') || out.startsWith('https://'))) {
    out = out.startsWith('/') ? out : `/api/${out}`;
  }
  // ⭐ 2026-10-05 资产自动刷新：带上版本令牌（后端 asset_gallery 下发的 mtime）。
  //   背景：资产重新生成后 URL 是不变的（/api/assets/<key>/<name>/base.png），
  //   轮询虽然换回了新 JSON，但 <img src> 字符串没变 —— React 不会重挂节点、
  //   浏览器还会命中旧缓存，用户看到的仍是旧图，误以为「必须手动刷新页面」。
  //   加上 ?v=<mtime> 后：图变了 URL 就变，React 必然重挂 <img>，浏览器必然重新取图。
  if (version !== undefined && version !== null && `${version}` !== '') {
    out += (out.includes('?') ? '&' : '?') + 'v=' + encodeURIComponent(String(version));
  }
  return out;
}

// ========== 错误脱敏（缺陷 D5） ==========
// 前端错误框只展示人话：丢掉 traceback / 模块名 / 文件路径等实现细节
function sanitizeError(err: unknown, fallback = t('wb.actionFailed')): string {
  const raw = typeof err === 'string' ? err : (err as any)?.message || '';
  let text = String(raw || '').trim();
  if (!text) return fallback;
  const tb = text.indexOf('Traceback (most recent call last)');
  if (tb !== -1) text = text.slice(0, tb).trim();
  const lines = text.split(/\r?\n/).filter(Boolean);
  text = (lines[lines.length - 1] || '').trim();
  text = text
    .replace(/File\s+"[^"]*",\s*line\s*\d+/g, '')
    .replace(/\s*from\s+'[^']*'/g, '')
    .replace(/\s*\([^()]*\.py[^()]*\)/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (/\.py\b|\bimport\b|\bmodule\b|site-packages|[\\/]/.test(text)) return fallback;
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
}

// ========== Overview Tab ==========

// 生产流水线步骤序列：与后端 pipeline.STEP_SEQUENCE 对齐（技术标识符 → i18n 键）。
// 2026-10-06：keyframe（尾帧）步骤已移出流水线 —— 7 步：script→tts_pre→assets→
// storyboard→video→upscale→final。
const PRODUCTION_STEPS: { id: string; labelKey: string }[] = [
  { id: 'script', labelKey: 'wb.stepScript' },
  { id: 'tts_pre', labelKey: 'wb.stepTtsPre' },
  { id: 'assets', labelKey: 'wb.stepAssets' },
  { id: 'storyboard', labelKey: 'wb.stepStoryboard' },
  { id: 'video', labelKey: 'wb.stepVideo' },
  { id: 'upscale', labelKey: 'wb.stepUpscale' },
  { id: 'final', labelKey: 'wb.stepFinal' },
];

/** 生产进度卡片：轮询 /api/autopilot/status，把「当前正在生产哪一集、当前步骤、百分比、
 *  超时告警」实时展示出来。此前这些数据后端都有，但前端从未渲染 —— 用户生产时只能干等。
 *  （报告 P1-5「无进度反馈」+ 建议 3「生产过程可视化」的落地） */
function ProductionProgress({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
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
    } catch {
      // 控制失败静默（轮询兜底刷新）
    } finally {
      setCtrlBusy(false);
    }
  };

  // ComfyUI 采样级实时进度：有生产任务时才轮询 comfyui 日志源（tqdm N/M）
  const comfy = useComfyProgress(!!cur);

  if (!cur) return null;

  const percent = Math.max(0, Math.min(100, Number(cur.percent) || 0));
  const stepsDone = Array.isArray(cur.steps_done) ? cur.steps_done : [];
  const stalled = Number(cur.step_stalled_sec) || 0;
  const stalledMin = Math.floor(stalled / 60);
  const stalledSec = stalled % 60;
  // step 可能是环节内子阶段（如 outline 提炼大纲属 script 步骤）——子阶段
  // 不在步骤表里，此时不点亮任何环节（百分比与描述行仍准确），避免步骤链错位
  const _rawStep = (cur.step || '').split(':')[0];
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
          {cur.episode != null && (
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
            <p className="font-semibold text-sm">{failurePause.reason || ('第' + (failurePause.episode ?? '') + '集生产失败，已暂停')}</p>
            {failurePause.error && (
              <p className="mt-1 break-all text-danger/80">{failurePause.error}</p>
            )}
            <p className="mt-1.5 text-danger/70">任务已自动暂停（stop_on_failure）。处理后点上方「恢复托管」继续；第{failurePause.episode ?? ''}集数据已保留，不会丢失。</p>
          </div>
          <button
            type="button"
            onClick={togglePause}
            className="shrink-0 inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium bg-danger text-white hover:opacity-90 transition-opacity"
          >
            <Play className="h-3 w-3" />
            {t('wb.resumeAutopilot')}
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
      {cur.message && <p className="text-xs text-ink-2 mb-1">{cur.message}</p>}

      {/* ComfyUI 采样进度（如「比例分镜 10/19」）：解析 comfyui 日志的 tqdm 行 */}
      {comfy.active && comfy.total > 0 && (
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
      {stalled >= 900 && (
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


/** 生产模式统一入口（2026-10-09 用户拍板合并）
 *
 * 背景：「托管」(/api/autopilot/*) 与「自主」(/api/autonomous/*) 是**同一件事的两套实现** ——
 *   托管：按既定计划逐集跑固定步骤，失败自动暂停，人可介入；
 *   自主：把决策权交给总控 AI（function-calling），由它自己判断下一步做什么。
 * 此前概览页上两个面板各有一个「启动」按钮，语义重叠、用户不知道点哪个。
 * 这里合并为**一个模式选择器**，同一时刻只呈现一种模式的控制面。
 */
function ProductionModeCard({ projectKey }: { projectKey: string }) {
  const [mode, setMode] = useState<'autopilot' | 'autonomous'>('autopilot');
  const seg = (active: boolean) =>
    `px-3 py-1 text-xs rounded transition-colors ${active
       ? 'bg-brand text-white font-medium'
       : 'text-ink-2 hover:bg-surface-2'}`;
  return (
    <div className="mb-4">
      <div className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-line bg-surface px-3 py-2">
        <span className="text-sm font-semibold text-ink-1">生产模式</span>
        <div className="inline-flex rounded-md border border-line p-0.5">
          <button type="button" className={seg(mode === 'autopilot')} onClick={() => setMode('autopilot')}>
            托管 · 按计划
          </button>
          <button type="button" className={seg(mode === 'autonomous')} onClick={() => setMode('autonomous')}>
            自主 · AI 决策
          </button>
        </div>
        <span className="min-w-0 flex-1 text-xs text-ink-3">
          {mode === 'autopilot'
            ? '按既定计划逐集生产，步骤固定；单集失败会自动暂停等你处理。'
            : '由总控 AI 自己判断下一步做什么（可调用工具），适合无人值守长跑。'}
        </span>
      </div>
      {mode === 'autopilot'
        ? <ProductionProgress projectKey={projectKey} />
        : <AutonomousPanel project={projectKey} />}
    </div>
  );
}

function OverviewTab({
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

  // --- 文学剧本（人审层，两段式第一步）：1章=1集，episode_no 即章号 ---
  const [spOpen, setSpOpen] = useState(false);
  const [spEpisode, setSpEpisode] = useState<number | null>(null);
  const [spContent, setSpContent] = useState('');
  const [spLoading, setSpLoading] = useState(false);
  const [spGenerating, setSpGenerating] = useState(false);
  const [spRewriting, setSpRewriting] = useState(false);
  // 2026-10-04 改写已自动化：本标志仅用于弹窗底部状态条展示「✅ 分镜剧本已生成」，
  // 打开弹窗/重新生成时重置（回看已有剧本永远走不到该状态）
  const [spRewritten, setSpRewritten] = useState(false);
  const [spError, setSpError] = useState('');
  // 标签切换会卸载本组件：卸载后让任务轮询让位，不再 setState
  const spAliveRef = useRef(true);
  useEffect(() => {
    spAliveRef.current = true;
    return () => { spAliveRef.current = false; };
  }, []);
  // 请求令牌：A 章生成中用户改点 B 章时，丢弃 A 的迟到结果，防止跨章串内容
  const spReqRef = useRef(0);

  /** 轮询 generation 任务至终态（3s 一次，15 分钟兜底）。
   *  ⚠️ /api/generation/status 历史上有 {success, task:{…}} 信封与顶层平铺两种形态，
   *  按 `task ?? 顶层` 兼容读取（与九宫格候选轮询同口径）。 */
  /** failMsg/timeoutMsg：场次重做等非剧本场景传入各自的错误文案（缺省回落剧本文案） */
  const pollGenerationTask = async (taskId: string, isCurrent: () => boolean, failMsg?: string, timeoutMsg?: string) => {
    const deadline = Date.now() + 15 * 60 * 1000;
    for (;;) {
      await new Promise(r => setTimeout(r, 3000));
      if (!spAliveRef.current || !isCurrent()) throw new Error(failMsg || t('wb.screenplayGenerateFailed'));
      const d: any = await generationApi.status(taskId);
      const task = (d?.task ?? d) || {};
      if (task.status === 'completed') return;
      if (task.status === 'failed' || task.status === 'cancelled') {
        throw new Error(task.error || failMsg || t('wb.screenplayGenerateFailed'));
      }
      if (Date.now() > deadline) throw new Error(timeoutMsg || t('wb.screenplayTimeout'));
    }
  };

  /** 每章行「文学剧本」：已有则直接弹窗展示；没有则发起生成（异步任务轮询）后再展示 */
  const openScreenplay = async (chapterNo: number) => {
    if (!novelId) return;
    const token = ++spReqRef.current;
    const isCurrent = () => token === spReqRef.current;
    setSpEpisode(chapterNo);
    setSpContent('');
    setSpError('');
    setSpGenerating(false);
    // 上一章迟到的改写请求被令牌作废后可能遗留 true：不许把改写态带进本章
    setSpRewriting(false);
    setSpRewritten(false);
    setSpOpen(true);
    setSpLoading(true);
    try {
      const doc = await screenplayApi.get(novelId, chapterNo, projectKey);
      if (!isCurrent()) return;
      if (doc.exists && doc.markdown) {
        // 回看已有剧本：只展示，不自动改写（避免回看场景重复烧 LLM）
        setSpContent(doc.markdown);
        setSpLoading(false);
        return;
      }
      // 不存在 → 生成（异步任务 + 轮询），完成后回读展示
      const r = await screenplayApi.generate(novelId, chapterNo, { projectName: projectKey });
      if (!r?.task_id) throw new Error(t('wb.screenplayStartFailed'));
      setSpLoading(false);
      setSpGenerating(true);
      await pollGenerationTask(r.task_id, isCurrent);
      if (!isCurrent()) return;
      const done = await screenplayApi.get(novelId, chapterNo, projectKey);
      if (!done.exists || !done.markdown) throw new Error(t('wb.screenplayNotGenerated'));
      setSpContent(done.markdown);
      // 2026-10-04 需求：文学剧本无需人工确认 —— 生成（轮询到 completed）后立即
      // 自动改写为分镜剧本，等价于旧版自动点「确认无误」按钮。先落 spGenerating
      // 再进改写，弹窗底部状态条才能依次显示「生成中 → 自动改写中 → 已完成」。
      // 改写失败由 runScreenplayRewrite 内部落在 spError，不进本层 catch。
      setSpGenerating(false);
      await runScreenplayRewrite(chapterNo, isCurrent);
    } catch (err) {
      if (isCurrent()) {
        setSpError(err instanceof Error ? err.message : t('wb.screenplayGenerateFailed'));
      }
    } finally {
      if (isCurrent()) {
        setSpLoading(false);
        setSpGenerating(false);
      }
    }
  };

  /** 两段式第二步：以文学剧本为原文重写成结构化分镜剧本。
   *  2026-10-04 起无需人工确认：「生成文学剧本」轮询到 completed 后由 openScreenplay
   *  自动调用本函数（等价于旧版自动点「确认无误」按钮）；弹窗底部确认按钮已改为
   *  流程状态展示，不再有手动入口。
   *  - chapter 显式传参：自动链路里同批 setState 尚未落地，读 spEpisode 会拿到旧章号；
   *  - stillCurrent 令牌守卫：自动链路传 openScreenplay 的 isCurrent，用户切章后迟到的
   *    改写结果不再动 UI；防重入由 spRewriting 兜底（改写中不允许再次触发）。 */
  const runScreenplayRewrite = async (chapter: number, stillCurrent?: () => boolean) => {
    if (!novelId || spRewriting) return;
    const alive = stillCurrent ?? (() => spAliveRef.current);
    setSpRewriting(true);
    setSpError('');
    setSpRewritten(false);
    try {
      const r = await episodesApi.generate(novelId, { chapters: [chapter], use_screenplay: true });
      // 兼容同步/异步两种返回：带 task_id 则轮询到完成
      const taskId = r?.task_id;
      if (taskId) {
        await pollGenerationTask(String(taskId), alive, t('wb.screenplayRewriteFailed'), t('wb.screenplayRewriteTimeout'));
      }
      if (!alive()) return;
      toast.success(t('wb.screenplayRewriteDone'));
      // 成功后弹窗保持打开，底部状态条显示「✅ 分镜剧本已生成」；剧集列表在背后刷新
      setSpRewritten(true);
      fetchEpisodes();
    } catch (err) {
      if (alive()) setSpError(err instanceof Error ? err.message : t('wb.screenplayRewriteFailed'));
    } finally {
      setSpRewriting(false);
    }
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
      // 与 openScreenplay 同款：组件已卸载（切标签页）就不再 setState / 刷新
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
    } finally {
      setDetailLoading(false);
    }
  };

  // 返回列表
  const goBack = () => {
    setSelectedEpisode(null);
    setEpisodeDetail(null);
    setDetailError('');
  };

  const groups: { key: 'characters' | 'items' | 'scenes'; label: string; icon: React.ReactNode; type: 'character' | 'item' | 'scene' }[] = [
    { key: 'characters', label: t('wb.characters'), icon: <User className="h-4 w-4" />, type: 'character' },
    { key: 'items', label: t('wb.items'), icon: <Box className="h-4 w-4" />, type: 'item' },
    { key: 'scenes', label: t('wb.scenes'), icon: <Mountain className="h-4 w-4" />, type: 'scene' },
  ];

  const total = groups.reduce((n, g) => n + (assets?.gallery?.[g.key]?.length || 0), 0);

  // 显示单集详情
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

        {/* 文学剧本（人审层）：默认收起，展开才拉取；novel_id 拿不到时整个面板不渲染。
            面板标题旁注明「此为人审稿」—— 下方「剧本内容」区块即改写后的结构化分镜剧本。 */}
        <ScreenplayPanel novelId={novelId} episodeNo={selectedEpisode} />

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
    // 硬失败：单集详情整块取不到数据，用 ErrorState 顶掉内容区（不是把已渲染内容盖掉）
    return (
      <div className="space-y-4">
        <ErrorState
          title={t('project.loadingFailed')}
          description={detailError}
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
    return (
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
    );
  }

  return (
    <div className="space-y-8">
      {/* 生产模式（托管 / 自主 二选一，见 ProductionModeCard）*/}
      <ProductionModeCard projectKey={projectKey} />


      {/* 资产展示 */}
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-lg font-semibold text-ink-1">{t('wb.assetsTitle')}</h3>
        <Button variant="secondary" onClick={() => setUploadOpen(true)}>
          <Upload className="h-4 w-4 mr-1.5" />
          {t('uploadSheet.entry')}
        </Button>
      </div>
      {total > 0 && (
        <>
          {groups.map((g) => {
            const list = assets?.gallery?.[g.key] || [];
            if (list.length === 0) return null;
            return (
              <div key={g.key}>
                <h3 className="text-sm font-semibold text-ink-2 mb-3 flex items-center gap-1.5">
                  {g.icon} {g.label} · {list.length}
                </h3>
                <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-4">
                  {list.map((item, idx) => (
                    <AssetCard
                      key={`${g.key}-${idx}`}
                      item={item}
                      type={g.type}
                      onClick={() => setPreview({ item, type: g.type })}
                    />
                  ))}
                </div>
              </div>
            );
          })}
          <AssetPreviewModal
            preview={preview}
            projectKey={projectKey}
            onClose={() => setPreview(null)}
            onRegenerated={onRefreshAssets}
          />
        </>
      )}
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
            description={scriptError}
            onRetry={fetchEpisodes}
          />
        ) : episodes.length === 0 ? (
          <EmptyState
            icon={<ClipboardList className="h-10 w-10" />}
            title={t('episodes.noEpisodes')}
            description={t('wb.startAutoFirst')}
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

            {/* 前置解析卡片：人物档案 + 故事梗概 + 关键事件 + 情绪基线 */}
            {preflightOpen && (
              <div className="bg-surface rounded-lg border border-line p-4 mb-4">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <h4 className="font-semibold text-ink-1">{t('wb.preflightTitle')}</h4>
                    <p className="text-xs text-ink-3 mt-0.5">{t('wb.preflightDesc')}</p>
                  </div>
                  <button onClick={() => setPreflightOpen(false)}
                    className={`p-1.5 rounded-md text-ink-3 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}>
                    <X className="h-4 w-4" />
                  </button>
                </div>
                {preflightDetail ? (
                  <div className="space-y-3">
                    <div className="text-sm text-ink-2">
                      <strong className="text-ink-1">{t('wb.preflightSummary')}</strong>
                      <p className="mt-1 text-ink-2">{preflightDetail.story_summary}</p>
                    </div>
                    {preflightDetail.key_events?.length > 0 && (
                      <div>
                        <strong className="text-sm text-ink-1">{t('wb.preflightKeyEvents')}</strong>
                        <ol className="mt-1 space-y-0.5 text-xs text-ink-2 list-decimal list-inside">
                          {preflightDetail.key_events.map((ev, i) => <li key={i}>{ev}</li>)}
                        </ol>
                      </div>
                    )}
                    {preflightDetail.characters?.length > 0 && (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                        {preflightDetail.characters.map(c => (
                          <div key={c.name} className="p-2.5 rounded-lg bg-surface-2 border border-line">
                            <div className="flex items-center gap-1.5 mb-1">
                              <span className="text-sm font-medium text-ink-1">{c.name}</span>
                              {c.gender && <span className="text-xs text-ink-3">{c.gender}</span>}
                              {c.identity && <span className="text-xs text-ink-3">· {c.identity}</span>}
                            </div>
                            {c.personality && (
                              <p className="text-xs text-ink-2"><strong>{t('wb.preflightPersonality')}：</strong>{c.personality}</p>
                            )}
                            {c.emotions?.length > 0 && (
                              <p className="text-xs text-ink-2 mt-0.5">
                                <strong>{t('wb.preflightEmotion')}</strong>{' '}
                                {c.emotions.slice(0, 4).map(e => e.emotion).join(' / ')}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="text-xs text-ink-3">{t('wb.preflightNoData')}</div>
                )}
              </div>
            )}
            {/* 剧集列表 */}
            <div className="bg-surface rounded-lg border border-line">
              <div className="p-4 border-b border-line">
                <div className="flex items-center justify-between">
                  <h4 className="font-semibold text-ink-1">{t('wb.episodeList')}</h4>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => (splitPlanOpen ? setSplitPlanOpen(false) : fetchSplitPlan())}
                      title={t('wb.splitPlanHint')}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${FOCUS_RING} ${
                        splitPlanOpen
                          ? 'bg-brand text-white'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1 border border-line'
                      }`}
                    >
                      {splitPlanOpen ? t('wb.splitPlanCollapse') : t('wb.splitPlan')}
                    </button>
                    <button
                      onClick={() => setPreflightOpen(v => !v)}
                      title={t('wb.preflightHint')}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${FOCUS_RING} ${
                        preflightOpen
                          ? 'bg-success text-white'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1 border border-line'
                      }`}
                    >
                      {t('wb.preflight')}
                    </button>
                  </div>
                </div>
                <p className="text-xs text-ink-2 mt-1">{t('wb.clickEpisodeHint')}</p>
              </div>

              {/* P2-2 分集断点提议卡片（与生产口径同源的只读预览） */}
              {splitPlanOpen && (
                <div className="px-4 py-4 border-b border-line bg-surface-2/50">
                  {splitPlanLoading && (
                    <div className="text-sm text-ink-2 flex items-center gap-2">
                      <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
                      {t('wb.splitPlanLoading')}
                    </div>
                  )}
                  {!splitPlanLoading && splitPlanError && (
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-sm text-danger-strong">{splitPlanError}</span>
                      <button
                        onClick={fetchSplitPlan}
                        className={`px-2.5 py-1 rounded-md text-xs bg-surface-2 text-ink-2 hover:bg-line ${FOCUS_RING}`}
                      >
                        {t('wb.splitPlanRetry')}
                      </button>
                    </div>
                  )}
                  {!splitPlanLoading && !splitPlanError && splitPlan && splitPlan.chapters?.length > 0 && (
                    <div className="space-y-3">
                      {/* 汇总行 */}
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="font-medium text-ink-1">
                          {t('wb.splitPlanTotal', { ep: splitPlan.total_episodes ?? 0, ch: splitPlan.chapter_count ?? splitPlan.chapters.length })}
                        </span>
                        {(splitPlan.needs_confirm_chapters?.length ?? 0) > 0 && (
                          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-warning-subtle text-warning-strong">
                            {t('wb.splitPlanNeedsConfirm', { n: splitPlan.needs_confirm_chapters.length })}
                          </span>
                        )}
                      </div>
                      {/* 逐章断点 */}
                      <div className="space-y-2">
                        {splitPlan.chapters.map((ch: any) => (
                          <div key={ch.index} className="bg-surface rounded-lg border border-line p-3">
                            <div className="flex flex-wrap items-center gap-2 mb-2">
                              <span className="text-sm font-semibold text-ink-1">
                                {t('wb.splitPlanChapter', { n: ch.index })}
                                {ch.title && <span className="ml-1.5 text-sm text-brand font-normal">{t('wb.splitPlanChapterTitle', { title: ch.title })}</span>}
                              </span>
                              <span className="text-xs text-ink-2">{t('wb.splitPlanParts', { n: ch.total_parts })}</span>
                              {ch.needs_confirm && (
                                <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-warning-subtle text-warning-strong">
                                  {t('wb.splitPlanConfirmNote')}
                                </span>
                              )}
                            </div>
                            {ch.units?.length > 0 && (
                              <div className="space-y-1">
                                {ch.units.map((u: any) => (
                                  <div key={u.part} className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
                                    <span className="font-medium text-ink-1 w-24 shrink-0">
                                      {t('wb.splitPlanPart', { part: u.part, total: ch.total_parts })}
                                    </span>
                                    <span className="tabular-nums">{t('wb.splitPlanShots', { n: u.est_shots ?? '—' })}</span>
                                    <span className="tabular-nums">{t('wb.splitPlanSec', { sec: u.est_sec ?? '—' })}</span>
                                    {u.over_redline && (
                                      <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-danger-subtle text-danger-strong">
                                        {t('wb.splitPlanOverRedline')}
                                      </span>
                                    )}
                                    {u.preview && (
                                      <span className="text-ink-3 truncate max-w-[14rem]">{u.preview}</span>
                                    )}
                                  </div>
                                ))}
                              </div>
                            )}
                            {ch.message && (
                              <p className="mt-1.5 text-xs text-ink-3">{ch.message}</p>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {!splitPlanLoading && !splitPlanError && splitPlan && (splitPlan.chapters?.length ?? 0) === 0 && (
                    <p className="text-sm text-ink-3">{t('wb.splitPlanNoChapters')}</p>
                  )}
                </div>
              )}

              {/* P2-15：补列表语义 —— 此前是一串裸 div/button，读屏不会播报
                  「列表，共 N 项」。这里刻意**不用** <table>：它是可点击的导航列表，
                  不是行列数据，套表格语义反而会误导读屏。 */}
              <ul className="divide-y divide-line">
                {episodes.map((ep: any) => (
                  <li key={ep.episode_no}>
                    <button
                      onClick={() => loadEpisodeDetail(ep.episode_no)}
                      className={`w-full p-4 hover:bg-surface-2 transition-colors text-left ${FOCUS_RING}`}
                    >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          {/* 场次层级（按场次生成）：展开/收起「第1场/第2场/…」；行本身仍点开单集详情 */}
                          <button
                            onClick={(e) => { e.stopPropagation(); toggleScenes(ep.episode_no); }}
                            aria-expanded={scenesOpenEps.includes(ep.episode_no)}
                            title={t('wb.scenesToggle')}
                            className={`shrink-0 rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-1 ${FOCUS_RING}`}
                          >
                            <svg className={`w-4 h-4 transition-transform ${scenesOpenEps.includes(ep.episode_no) ? 'rotate-90' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                            </svg>
                          </button>
                          <span className="flex items-center justify-center w-8 h-8 rounded-full bg-brand-subtle text-brand text-sm font-semibold">
                            {ep.episode_no}
                          </span>
                          <div>
                            <div className="flex items-center gap-1.5">
                              <p className="font-medium text-ink-1">
                                {t('wb.episodeNo', { n: ep.episode_no })}
                                {ep.chapter_title && <span className="ml-2 text-sm text-brand">《{ep.chapter_title}》</span>}
                              </p>
                              {/* 前置解析状态：绿点=已完成（点击查看） / 灰点=可运行 / 转圈=运行中 */}
                              {preflightDone.includes(ep.episode_no) && (
                                <button
                                  onClick={e => { e.stopPropagation(); openPreflightDetail(ep.episode_no); }}
                                  title={t('wb.preflightView')}
                                  className="h-2.5 w-2.5 rounded-full bg-success border-0 p-0 cursor-pointer"
                                />
                              )}
                              {preflightRunning === ep.episode_no && (
                                <div className="w-3 h-3 border-2 border-brand border-t-transparent rounded-full animate-spin" />
                              )}
                              {!preflightDone.includes(ep.episode_no) && preflightRunning !== ep.episode_no && (
                                <button
                                  onClick={e => { e.stopPropagation(); runPreflight(ep.episode_no); }}
                                  title={t('wb.preflightRun')}
                                  className="h-2.5 w-2.5 rounded-full bg-line border border-ink-3 cursor-pointer hover:bg-brand transition-colors"
                                />
                              )}
                            </div>
                            <p className="text-xs text-ink-2 mt-0.5">
                              {t('wb.chapterNo', { n: ep.chapter_index ?? ep.episode_no })}
                            </p>
                          </div>
                        </div>

                        <div className="flex items-center gap-4">
                          {/* 文学剧本（人审层）：查看/生成本章文学剧本，确认后可改写为分镜剧本。
                              行本身是可点击的大按钮，这里必须 stopPropagation 防止误开详情 */}
                          <button
                            onClick={(e) => { e.stopPropagation(); openScreenplay(ep.episode_no); }}
                            disabled={spGenerating && spEpisode === ep.episode_no}
                            title={t('wb.screenplayRowHint')}
                            className={`inline-flex shrink-0 items-center gap-1 rounded-md border border-line bg-surface-2 px-2 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-line hover:text-ink-1 disabled:opacity-60 ${FOCUS_RING}`}
                          >
                            <FileText className="h-3.5 w-3.5" />
                            {t('wb.screenplay')}
                          </button>
                          <div className="text-right">
                            <div className="text-sm text-ink-2">
                              {t('wb.shotsRatio', { done: ep.completed_shots, total: ep.shot_count })}
                            </div>
                            {ep.created_at && (
                              <div className="text-xs text-ink-3">{ep.created_at.split('T')[0]}</div>
                            )}
                          </div>

                          <span className={`px-2 py-1 rounded-full text-xs font-medium inline-flex items-center gap-1 ${
                            ep.status === 'done' ? 'bg-success-subtle text-success-strong' :
                            ep.status === 'producing' ? 'bg-info-subtle text-info-strong' :
                            ep.status === 'failed' ? 'bg-danger-subtle text-danger-strong' :
                            'bg-surface-2 text-ink-1'
                          }`}>
                            {ep.status === 'done' ? (<><Check className="h-3.5 w-3.5" /> {t('wb.done')}</>) :
                             ep.status === 'producing' ? t('wb.producing') :
                             ep.status === 'failed' ? (<><X className="h-3.5 w-3.5" /> {t('episodes.failed')}</>) :
                             t('ep.pending')}
                          </span>

                          {/* 按场次生成：该集全部场次视频已拼接成整集成片（epNN_full.mp4） */}
                          {scenesByEp[ep.episode_no]?.full_video_ready && (
                            <span className="inline-flex items-center gap-1 rounded-full bg-success-subtle px-2 py-1 text-xs font-medium text-success-strong">
                              <Check className="h-3.5 w-3.5" /> {t('wb.fullVideoReady')}
                            </span>
                          )}

                          <svg className="w-5 h-5 text-ink-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                          </svg>
                        </div>
                      </div>

                      {ep.shot_count > 0 && (
                        <div className="mt-3 w-full bg-line rounded-full h-1.5">
                          <div
                            className={`h-1.5 rounded-full transition-all ${
                              ep.status === 'done' ? 'bg-success' :
                              ep.status === 'failed' ? 'bg-danger' :
                              'bg-brand'
                            }`}
                            style={{ width: `${(ep.completed_shots / ep.shot_count) * 100}%` }}
                          ></div>
                        </div>
                      )}
                    </button>

                    {/* 场次子列表（按场次生成）：第1场/第2场/… + 分镜/视频状态 + 重做本场。
                        既有「点集名展开逐镜详情」不受影响 —— 本面板是新增的场次层级。 */}
                    {scenesOpenEps.includes(ep.episode_no) && (
                      <div className="border-t border-line bg-surface-2/50 px-4 py-3">
                        {scenesLoadingEp === ep.episode_no && !scenesByEp[ep.episode_no] ? (
                          <div className="flex items-center gap-2 py-1 text-sm text-ink-2">
                            <div className="h-4 w-4 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                            {t('wb.scenesLoading')}
                          </div>
                        ) : scenesErrByEp[ep.episode_no] ? (
                          <div className="flex items-center justify-between gap-3 py-1">
                            <span className="text-sm text-danger-strong">{scenesErrByEp[ep.episode_no]}</span>
                            <button
                              onClick={() => fetchScenes(ep.episode_no)}
                              className={`rounded-md bg-surface-2 px-2.5 py-1 text-xs text-ink-2 transition-colors hover:bg-line ${FOCUS_RING}`}
                            >
                              {t('wb.scenesRetry')}
                            </button>
                          </div>
                        ) : (scenesByEp[ep.episode_no]?.scenes?.length ?? 0) === 0 ? (
                          <p className="py-1 text-sm text-ink-3">{t('wb.scenesEmpty')}</p>
                        ) : (
                          <ul className="divide-y divide-line">
                            {(scenesByEp[ep.episode_no]?.scenes || []).map((sc) => {
                              const redoKey = `${ep.episode_no}:${sc.scene_no}`;
                              return (
                                <li key={sc.scene_no} className="flex flex-wrap items-center gap-2 py-2.5">
                                  <span className="shrink-0 text-sm font-medium text-ink-1">
                                    {t('wb.sceneNo', { n: sc.scene_no })}
                                  </span>
                                  {sc.heading && (
                                    <span className="max-w-[18rem] truncate text-xs text-ink-2" title={sc.heading}>{sc.heading}</span>
                                  )}
                                  {sc.location && (
                                    <span className="text-xs text-ink-3">{sc.location}</span>
                                  )}
                                  <span className="tabular-nums text-xs text-ink-2">{t('wb.sceneShotCount', { n: sc.shot_count })}</span>
                                  {/* 分镜状态：齐了亮绿「分镜完成」，否则显示「分镜 k/N」 */}
                                  {sc.shot_count > 0 && sc.storyboard_ok >= sc.shot_count ? (
                                    <span className="rounded bg-success-subtle px-1.5 py-0.5 text-xs font-medium text-success-strong">
                                      {t('wb.sceneStoryboardDone')}
                                    </span>
                                  ) : (
                                    <span className="rounded bg-warning-subtle px-1.5 py-0.5 text-xs font-medium text-warning-strong">
                                      {t('wb.sceneStoryboardPart', { done: sc.storyboard_ok, total: sc.shot_count })}
                                    </span>
                                  )}
                                  {/* 视频状态：该场 scene_XX.mp4 是否已生成 */}
                                  {sc.video_ready ? (
                                    <span className="rounded bg-success-subtle px-1.5 py-0.5 text-xs font-medium text-success-strong">
                                      ✓ {t('wb.sceneVideoReady')}
                                    </span>
                                  ) : (
                                    <span className="rounded bg-surface-2 px-1.5 py-0.5 text-xs font-medium text-ink-3">
                                      {t('wb.sceneVideoMissing')}
                                    </span>
                                  )}
                                  <button
                                    onClick={() => redoScene(ep.episode_no, sc.scene_no)}
                                    disabled={redoBusy !== null}
                                    title={t('wb.sceneRedoHint')}
                                    className={`ml-auto inline-flex shrink-0 items-center gap-1 rounded-md border border-line bg-surface px-2 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-line hover:text-ink-1 disabled:opacity-60 ${FOCUS_RING}`}
                                  >
                                    {redoBusy === redoKey && (
                                      <span className="h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                                    )}
                                    {redoBusy === redoKey ? t('wb.sceneRedoBusy') : t('wb.sceneRedo')}
                                  </button>
                                </li>
                              );
                            })}
                          </ul>
                        )}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
      </div>

      {/* 文学剧本弹窗（人审层）：展示/生成本章文学剧本；生成完成后自动改写为分镜剧本
          （2026-10-04 起无需人工确认，弹窗底部为流程状态条而非确认按钮） */}
      <ScreenplayModal
        isOpen={spOpen}
        episodeNo={spEpisode}
        content={spContent}
        loading={spLoading}
        generating={spGenerating}
        rewriting={spRewriting}
        rewritten={spRewritten}
        error={spError}
        onClose={() => setSpOpen(false)}
      />
    </div>
  );
}

// ========== 文学剧本弹窗（人审层，两段式第一步） ==========
// 展示 / 生成本章文学剧本。正文为 Markdown 文本：项目无 markdown 渲染依赖
// （package.json 仅 react/react-dom/react-router），按约定用 whitespace-pre-wrap
// 的正文样式直接展示，不引入新依赖。
// 2026-10-04 起两段式第二步自动化：生成完成后由 OverviewTab 自动触发改写，
// 底部不再是「确认无误」按钮，而是流程状态条（生成中 → 自动改写中 → 已完成）；
// 失败在正文上方红色块展示。回看已有剧本不自动改写，底部只留「关闭」。
function ScreenplayModal({
  isOpen,
  episodeNo,
  content,
  loading,
  generating,
  rewriting,
  rewritten,
  error,
  onClose,
}: {
  isOpen: boolean;
  episodeNo: number | null;
  content: string;
  loading: boolean;
  generating: boolean;
  rewriting: boolean;
  rewritten: boolean;
  error: string;
  onClose: () => void;
}) {
  const { t } = useApp();
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      preventClose={rewriting}
      title={t('wb.screenplayModalTitle', { n: episodeNo ?? '' })}
      description={t('wb.screenplayModalDesc')}
      size="xl"
      footer={
        <div className="flex w-full flex-wrap items-center gap-2">
          {/* 流程状态条：正在生成文学剧本… → 正在自动改写为分镜剧本… → ✅ 已生成
              （三个状态互斥；失败走正文上方红色错误块，此处恢复只读「关闭」） */}
          {generating && (
            <span role="status" className="inline-flex items-center gap-1.5 text-sm text-ink-2">
              <span className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" />
              {t('wb.screenplayAutoGenerating')}
            </span>
          )}
          {rewriting && (
            <span role="status" className="inline-flex items-center gap-1.5 text-sm text-ink-2">
              <span className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-brand border-t-transparent" />
              {t('wb.screenplayAutoRewriting')}
            </span>
          )}
          {!generating && !rewriting && rewritten && (
            <span role="status" className="inline-flex items-center gap-1.5 text-sm font-medium text-success-strong">
              <CheckCircle2 className="h-4 w-4 shrink-0" />
              {t('wb.screenplayAutoDone')}
            </span>
          )}
          <Button variant="secondary" onClick={onClose} disabled={rewriting} className="ml-auto">
            {t('common.close')}
          </Button>
        </div>
      }
    >
      <div className="space-y-3">
        {error && (
          <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm break-words">
            {error}
          </div>
        )}
        {loading ? (
          <Loading size="md" label={t('common.loading')} />
        ) : generating ? (
          <Loading size="md" label={t('wb.screenplayGenerating')} />
        ) : content ? (
          <pre className="max-h-[60vh] overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-surface-2 p-4 text-sm leading-6 text-ink-1">
            {content}
          </pre>
        ) : (
          <p className="text-sm text-ink-2">{t('wb.screenplayNotGenerated')}</p>
        )}
      </div>
    </Modal>
  );
}

// ========== 文学剧本折叠面板（单集详情，人审层） ==========
// 默认收起、展开才拉取（与资产沉淀/服装变体面板同款 fail-open：接口异常降级为
// 一行提示，绝不打断详情主体）。novel_id 拿不到时整个面板不渲染，零报错。
// 下方既有「剧本内容」区块即改写后的结构化分镜剧本，两者关系在标题旁注明。
function ScreenplayPanel({ novelId, episodeNo }: { novelId?: string; episodeNo: number | null }) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const [markdown, setMarkdown] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // 换集重置：上一集的剧本绝不能带进下一集
  useEffect(() => {
    setOpen(false);
    setMarkdown('');
    setError('');
  }, [episodeNo]);

  useEffect(() => {
    if (!open || !novelId || episodeNo == null) return;
    let alive = true;
    setLoading(true);
    setError('');
    screenplayApi.get(novelId, episodeNo)
      .then((d) => {
        if (!alive) return;
        if (d?.exists && d.markdown) setMarkdown(d.markdown);
        else setError(t('wb.screenplayNotGenerated'));
      })
      .catch(() => { if (alive) setError(t('wb.screenplayLoadFailed')); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // t 是 i18n 稳定引用；按 open/novelId/episodeNo 触发即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, novelId, episodeNo]);

  if (!novelId || episodeNo == null) return null;

  return (
    <div className="bg-surface rounded-lg border border-line p-6">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        className={`flex w-full items-center justify-between gap-2 rounded text-left ${FOCUS_RING}`}
      >
        <span className="flex min-w-0 flex-wrap items-center gap-2">
          <FileText className="h-4 w-4 shrink-0 text-ink-2" />
          <span className="font-semibold text-ink-1">{t('wb.screenplay')}</span>
          <span className="text-xs font-normal text-ink-3">{t('wb.screenplayHumanTag')}</span>
        </span>
        <span className="shrink-0 text-xs text-ink-3">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="mt-3">
          {loading ? (
            <div className="text-sm text-ink-3">{t('common.loading')}</div>
          ) : error ? (
            <div className="text-sm text-ink-2">{error}</div>
          ) : (
            <pre className="max-h-[50vh] overflow-y-auto whitespace-pre-wrap rounded-md border border-line bg-surface-2 p-4 text-sm leading-6 text-ink-1">
              {markdown}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// ========== Asset Card ==========
function AssetCard({
  item,
  type,
  onClick,
}: {
  item: AssetItem;
  type: 'character' | 'item' | 'scene';
  onClick?: () => void;
}) {
  const { t } = useApp();
  const _v = item.thumb || item.views?.[0];
  const imageUrl = assetSrc(_v?.url, _v?.mtime ?? _v?.size);
  const [broken, setBroken] = useState(false);
  const FallbackIcon = type === 'character' ? User : type === 'item' ? Box : Mountain;
  // 缩略图容器比例必须跟随资产实际画幅（后端 style_kit.ASSET_BASE_RATIO 写死）：
  // 角色三视图设定图与道具图是 1:1、场景原画是 16:9。此前一律 aspect-video + object-cover，
  // 一张 1:1（或更早的 3:4 竖幅）三视图放进 16:9 容器会被裁掉上下两边 ——
  // 人物头顶与脚底同时被切，看起来就是「三视图比例不对」。
  const thumbAspect = type === 'scene' ? 'aspect-video' : 'aspect-square';

  return (
    <button
      type="button"
      onClick={onClick}
      className={`text-left bg-surface rounded-xl border border-line overflow-hidden hover:shadow-lg transition-shadow ${FOCUS_RING}`}
    >
      <div className={`${thumbAspect} bg-surface-2 flex items-center justify-center overflow-hidden`}>
        {imageUrl && !broken ? (
          <img
            src={imageUrl}
            alt={item.name}
            loading="lazy"
            className="w-full h-full object-cover"
            onError={() => setBroken(true)}
          />
        ) : (
          <FallbackIcon className="h-9 w-9 text-ink-3" />
        )}
      </div>
      <div className="p-3">
        <h4 className="font-medium text-ink-1 text-sm truncate">{item.name}</h4>
        <p className="text-xs text-ink-2 mt-1">
          {item.category || (item.view_count ? t('wb.viewCount', { n: item.view_count }) : type === 'character' ? t('wb.characters') : type === 'item' ? t('wb.items') : t('wb.scenes'))}
        </p>
      </div>
    </button>
  );
}

// ========== Upload Character Sheet Modal（上传形象图 → 三视图） ==========
// 设计要点：上传的图**直接落 base.png** 再本地切分（零 GPU、零质检），所以
//   · 界面必须先把「期望版式」画清楚 —— 用户按版式出图才切得开；
//   · 切分失败不能报模糊错误，要把后端给的 layout_hint 原样透出来；
//   · 角色名给下拉（从项目剧本/已有资产取），避免手打错字落错目录。
function UploadSheetModal({
  isOpen,
  onClose,
  projectKey,
  characters,
  onUploaded,
}: {
  isOpen: boolean;
  onClose: () => void;
  projectKey: string;
  characters: string[];
  onUploaded?: () => void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [character, setCharacter] = useState('');
  const [customName, setCustomName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState('');
  const [busy, setBusy] = useState(false);
  const [overwrite, setOverwrite] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 每次打开重置：上一次的文件/错误绝不能带进下一次（会误传给另一个角色）
  useEffect(() => {
    if (!isOpen) return;
    setFile(null);
    setPreview('');
    setBusy(false);
    setOverwrite(false);
    setCustomName('');
    setCharacter((prev) => (prev && characters.includes(prev) ? prev : characters[0] || ''));
  }, [isOpen, characters]);

  useEffect(() => {
    if (!file) {
      setPreview('');
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const finalName = (character === '__custom__' ? customName : character).trim();

  const submit = async () => {
    if (!finalName) {
      toast.error(t('uploadSheet.needCharacter'));
      return;
    }
    if (!file) {
      toast.error(t('uploadSheet.needFile'));
      return;
    }
    setBusy(true);
    try {
      const r = await characterSheetUpload.upload({
        project_name: projectKey,
        character: finalName,
        file,
        overwrite,
      });
      if (r.skipped) {
        toast.info(r.message || t('uploadSheet.skipped'));
      } else {
        // 2026-10-02 不裁剪：views 恒为空，改用后端返回的准确文案（否则会显示「0 张视角图」）
        toast.success(r.message || t('uploadSheet.ok', { n: Object.keys(r.views || {}).length }));
      }
      onUploaded?.();
      onClose();
    } catch (e) {
      // 后端把「版式不符」的可读原因 + layout_hint 都放在 error 里，直接展示
      toast.error(e instanceof Error ? e.message : t('uploadSheet.failed'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('uploadSheet.title')}>
      <div className="space-y-4">
        {/* 版式示意：这是本功能唯一的使用门槛，必须一眼看懂 */}
        <div className="rounded-lg border border-line bg-surface-2 p-3">
          <p className="text-xs font-medium text-ink-1 mb-2">{t('uploadSheet.layoutTitle')}</p>
          <div className="flex items-center gap-2">
            <div className="flex flex-col gap-1">
              <div className="flex gap-1">
                {['front', 'left', 'back'].map((k) => (
                  <div
                    key={k}
                    className="w-12 h-16 rounded border border-dashed border-ink-3 bg-surface flex items-center justify-center text-xs text-ink-3"
                  >
                    {t(`uploadSheet.view.${k}`)}
                  </div>
                ))}
              </div>
              <div className="w-12 h-16 rounded border border-dashed border-ink-3 bg-surface flex items-center justify-center text-xs text-ink-3">
                {t('uploadSheet.view.half')}
              </div>
            </div>
            <p className="text-xs text-ink-2 flex-1">{t('uploadSheet.layoutHint')}</p>
          </div>
        </div>

        {/* 角色选择 */}
        <div>
          <label className="block text-sm font-medium text-ink-1 mb-1">
            {t('uploadSheet.character')}
          </label>
          <Select
            value={character === '__custom__' || !characters.includes(character) ? '__custom__' : character}
            onChange={(v) => setCharacter(v)}
            options={[
              ...characters.map((c) => ({ value: c, label: c })),
              { value: '__custom__', label: t('uploadSheet.customName') },
            ]}
            className="w-full"
          />
          {(character === '__custom__' || characters.length === 0) && (
            <Input
              className="mt-2 w-full"
              value={customName}
              placeholder={t('uploadSheet.characterPlaceholder')}
              onChange={(v) => setCustomName(v)}
            />
          )}
        </div>

        {/* 文件选择 */}
        <div>
          <label className="block text-sm font-medium text-ink-1 mb-1">
            {t('uploadSheet.file')}
          </label>
          <input
            ref={fileRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
          <div className="flex items-center gap-3">
            <Button variant="secondary" onClick={() => fileRef.current?.click()} disabled={busy}>
              {file ? t('uploadSheet.reselect') : t('uploadSheet.choose')}
            </Button>
            <span className="text-xs text-ink-2 truncate">
              {file ? `${file.name}（${(file.size / 1024).toFixed(0)} KB）` : t('uploadSheet.noFile')}
            </span>
          </div>
          {preview && (
            <div className="mt-2 rounded-lg border border-line overflow-hidden bg-surface-2">
              <img src={preview} alt="preview" className="w-full max-h-56 object-contain" />
            </div>
          )}
        </div>

        <label className="flex items-center gap-2 text-sm text-ink-2">
          <input
            type="checkbox"
            checked={overwrite}
            onChange={(e) => setOverwrite(e.target.checked)}
            className="rounded border-line"
          />
          {t('uploadSheet.overwrite')}
        </label>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button onClick={submit} disabled={busy || !file || !finalName}>
            {busy ? t('uploadSheet.uploading') : t('uploadSheet.submit')}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// ========== Asset Preview Modal ==========
// 薄封装：遮罩、头部、动画、ESC / 遮罩关闭、滚动锁定、焦点陷阱、层级全部由共享 Modal
// 负责（方案 P1-7）。此前这里是一份独立的自建弹层（bg-black/60 + p-4 头部 + z-modal），
// 与全站 Modal 的观感和层级都对不上。本组件只保留资产预览自己的业务：多视角切换 + 下载。
function AssetPreviewModal({
  preview,
  projectKey,
  onClose,
  onRegenerated,
}: {
  preview: { item: AssetItem; type: 'character' | 'item' | 'scene' } | null;
  projectKey: string;
  onClose: () => void;
  onRegenerated?: () => void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [active, setActive] = useState(0);
  const isOpen = !!preview;

  // —— 提示词：打开时拉取详情接口（/api/projects/<pid>/asset-detail），展示可编辑的
  //    生成提示词，支持「改提示词 → 单点重新生成」。此前详情接口后端早已返回
  //    meta.prompt_zh 与 regenerate 模板，但前端从未渲染，用户只能看整图无从改起。
  const [promptZh, setPromptZh] = useState('');
  const [promptEn, setPromptEn] = useState('');
  const [promptLoading, setPromptLoading] = useState(false);
  const [regenerating, setRegenerating] = useState(false);

  useEffect(() => {
    setActive(0);
    setPromptZh('');
    setPromptEn('');
    if (!preview) return;
    let alive = true;
    setPromptLoading(true);
    fetch(
      `/api/projects/${encodeURIComponent(projectKey)}/asset-detail` +
        `?kind=${encodeURIComponent(preview.type)}&name=${encodeURIComponent(preview.item.name)}`
    )
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        if (!alive) return;
        setPromptZh(d?.meta?.prompt_zh || '');
        setPromptEn(d?.meta?.prompt_en || '');
      })
      .catch(() => {
        if (alive) {
          setPromptZh('');
          setPromptEn('');
        }
      })
      .finally(() => { if (alive) setPromptLoading(false); });
    return () => { alive = false; };
  }, [preview, projectKey]);

  const gallery = React.useMemo(() => {
    if (!preview) return [] as { url: string; view?: string; size?: number; mtime?: number }[];
    return [
      preview.item.thumb,
      ...(preview.item.views || []),
    ].filter(Boolean) as { url: string; view?: string; size?: number; mtime?: number }[];
  }, [preview]);

  // ← / → 在多个视角之间切换（图片浏览器的最低预期）
  useEffect(() => {
    if (!isOpen || gallery.length <= 1) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowRight') setActive((i) => (i + 1) % gallery.length);
      else if (e.key === 'ArrowLeft') setActive((i) => (i - 1 + gallery.length) % gallery.length);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, gallery.length]);

  // 共享 Modal 已处理 isOpen=false 时不渲染，这里只需容忍 preview 为空时的取值
  const item = preview?.item;
  const current = gallery[active];
  const src = assetSrc(current?.url, current?.mtime ?? current?.size);

  const downloadCurrent = () => {
    if (!src) return;
    const a = document.createElement('a');
    a.href = src;
    a.download = `${item?.name || 'asset'}${current?.view ? '_' + current.view : ''}.png`;
    a.click();
  };

  // 单点重新生成：用当前编辑后的提示词覆盖重新出图（overwrite=true）。
  const regenerate = async () => {
    if (!preview || regenerating) return;
    const zh = promptZh.trim();
    if (!zh) {
      toast.warning(t('wb.assetPromptRequired'));
      return;
    }
    setRegenerating(true);
    try {
      const res = await fetch('/api/assets/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          asset_type: preview.type,
          project_name: projectKey,
          overwrite: true,
          assets: [{
            name: item?.name,
            reference_prompt_zh: zh,
            reference_prompt_en: promptEn.trim(),
          }],
        }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || !d.task_id) {
        toast.error(d?.error || t('wb.assetRegenerateFailed'));
      } else {
        toast.success(t('wb.assetRegenerateStarted'));
        onRegenerated?.();
      }
    } catch {
      toast.error(t('wb.assetRegenerateFailed'));
    } finally {
      setRegenerating(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={item?.name || t('wb.assetPreview')}
      size="xl"
      footer={
        <div className="flex w-full items-center gap-2">
          <Button size="sm" variant="secondary" onClick={downloadCurrent}>
            {t('wb.downloadCurrent')}
          </Button>
          <Button
            size="sm"
            variant="brand"
            onClick={regenerate}
            disabled={regenerating || promptLoading || !promptZh.trim()}
          >
            {regenerating ? t('wb.assetRegenerating') : t('wb.assetRegenerate')}
          </Button>
          {current?.size && (
            <span className="text-xs text-ink-3">{(current.size / 1024).toFixed(0)} KB</span>
          )}
          {gallery.length > 1 && (
            <span className="ml-auto text-xs text-ink-3">{t('wb.switchView')}</span>
          )}
        </div>
      }
    >
        <div className="space-y-4">
          {src ? (
            <img
              src={src}
              alt={`${item?.name || ''}${current?.view ? ` - ${current.view}` : ''}`}
              className="w-full rounded-md bg-surface-2"
            />
          ) : (
            <EmptyState icon={<ImageIcon className="h-10 w-10" />} title={t('wb.imageUnavailable')} />
          )}

          {gallery.length > 1 && (
            <div className="flex flex-wrap gap-2" role="tablist" aria-label={t('wb.viewSwitch')}>
              {gallery.map((g, i) => {
                const thumb = assetSrc(g.url, g.mtime ?? g.size);
                return (
                  <button
                    key={i}
                    type="button"
                    role="tab"
                    aria-selected={i === active}
                    aria-label={g.view || t('wb.viewN', { n: i + 1 })}
                    onClick={() => setActive(i)}
                    className={`h-14 w-20 overflow-hidden rounded border-2 ${FOCUS_RING} ${
                      i === active ? 'border-brand' : 'border-transparent hover:border-line-strong'
                    }`}
                  >
                    {thumb && <img src={thumb} alt="" className="h-full w-full object-cover" />}
                  </button>
                );
              })}
            </div>
          )}

          {/* 生成提示词：可编辑 + 单点重新生成 */}
          <div className="border border-line rounded-lg p-3 space-y-2">
            <h4 className="text-sm font-semibold text-ink-1">{t('wb.assetPrompt')}</h4>
            {promptLoading ? (
              <div className="text-xs text-ink-3">{t('common.loading')}</div>
            ) : (
              <>
                <textarea
                  value={promptZh}
                  onChange={(e) => setPromptZh(e.target.value)}
                  rows={4}
                  placeholder={t('wb.assetPromptPlaceholder')}
                  className={`w-full rounded-md border border-line bg-surface px-3 py-2 text-sm text-ink-1 resize-y ${FOCUS_RING}`}
                />
                <p className="text-xs text-ink-3">{t('wb.assetPromptHint')}</p>
              </>
            )}
          </div>

          {/* 资产沉淀过程（时间线）：抽取 → 提示词 → 出图 → 质检 → 教训 → 切分 → 入库 */}
          {item?.name && (
            <AssetPrecipitationSection
              projectKey={projectKey}
              kind={preview?.type || 'character'}
              name={item.name}
            />
          )}

          {/* 服装变体（衣柜）：仅角色资产显示 —— 列表 + 新增入口 */}
          {preview?.type === 'character' && item?.name && (
            <CharacterOutfitsSection projectKey={projectKey} character={item.name} />
          )}
        </div>
    </Modal>
  );
}

// ========== 资产沉淀过程（时间线，2026-10-06） ==========
// 需求：资产不是「一下就有的」，用户在界面上只能看到「最后那张图」，看不到
// 「它被改了几次、为什么改、学到了什么」。后端 /api/projects/<pid>/asset-precipitation
// 把散落在质检历史（QC_DIR）、产物旁路元数据（*.meta.json）、教训库
// （output/lessons/lessons.jsonl）三处的痕迹按资产聚合成 7 步时间线。
//
// 与 CharacterOutfitsSection 同款 fail-open：默认折叠、点开才拉取；接口异常降级成
// 「无法读取」一行字，绝不打断预览主体（后端零回归约束对齐）。
function AssetPrecipitationSection({
  projectKey,
  kind,
  name,
}: {
  projectKey: string;
  kind: string;
  name: string;
}) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<AssetPrecipitationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    setData(null);
    setError(false);
    if (!open || !projectKey || !name) return;
    let alive = true;
    setLoading(true);
    assetPrecipitation
      .get(projectKey, kind, name)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setError(true); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [open, projectKey, kind, name]);

  // 状态 → 圆点样式 + 文案。skipped/pending 视觉上明显弱于 done/failed，
  // 因为它们是「本来就没有这一步」而非「出错了」，不该让用户误以为有问题。
  const dotOf = (status: PrecipitationStatus) => {
    switch (status) {
      case 'done':    return 'bg-success-strong border-success-strong';
      case 'failed':  return 'bg-danger-strong border-danger-strong';
      case 'pending': return 'bg-surface border-line-strong';
      default:        return 'bg-surface-2 border-line';
    }
  };
  const badgeOf = (status: PrecipitationStatus) => {
    switch (status) {
      case 'done':    return 'bg-success-subtle text-success-strong';
      case 'failed':  return 'bg-danger-subtle text-danger-strong';
      default:        return 'bg-surface-2 text-ink-3';
    }
  };
  const statusText = (status: PrecipitationStatus) =>
    t(`precip.status.${status}`);

  const summary = data?.summary;

  return (
    <div className="border border-line rounded-lg p-3 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`flex w-full items-center justify-between rounded text-sm font-semibold text-ink-1 hover:text-brand transition-colors ${FOCUS_RING}`}
      >
        <span className="flex items-center gap-2">
          <span>{t('precip.title')}</span>
          {summary && (
            <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs font-normal text-ink-3">
              {t('precip.summary.done', { done: summary.done, total: summary.total_steps })}
            </span>
          )}
        </span>
        <span className="text-xs text-ink-3">{open ? '−' : '+'}</span>
      </button>

      {open && (
        <div className="space-y-3">
          {loading ? (
            <div className="text-xs text-ink-3">{t('common.loading')}</div>
          ) : error || !data ? (
            <div className="text-xs text-ink-3">{t('precip.loadFailed')}</div>
          ) : (
            <>
              {/* 概览：质检次数 / 命中教训 / 视角数 / 是否用户上传 */}
              <div className="flex flex-wrap gap-1.5">
                {summary?.is_user_upload && (
                  <span className="rounded-full bg-brand-subtle px-2 py-0.5 text-xs font-medium text-brand-strong">
                    {t('precip.badge.userUpload')}
                  </span>
                )}
                <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-ink-2">
                  {t('precip.stat.qc', { n: summary?.qc_attempts ?? 0 })}
                </span>
                <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-ink-2">
                  {t('precip.stat.lessons', { n: summary?.lessons ?? 0 })}
                </span>
                {!!summary?.views?.length && (
                  <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-ink-2">
                    {t('precip.stat.views', { n: summary.views.length })}
                  </span>
                )}
              </div>

              {/* 步骤时间线 */}
              <ol className="space-y-0">
                {data.steps.map((s, i) => (
                  <li key={s.id} className="flex gap-2.5">
                    {/* 竖线 + 圆点 */}
                    <span className="flex flex-col items-center pt-1.5">
                      <span className={`h-2.5 w-2.5 shrink-0 rounded-full border-2 ${dotOf(s.status)}`} />
                      {i < data.steps.length - 1 && <span className="w-px flex-1 bg-line" />}
                    </span>
                    <div className="min-w-0 flex-1 pb-3">
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-ink-1">{s.label}</span>
                        <span
                          className={`shrink-0 rounded-full px-1.5 py-0.5 text-xs font-medium ${badgeOf(s.status)}`}
                        >
                          {statusText(s.status)}
                        </span>
                      </div>
                      {s.detail && (
                        <p className="mt-0.5 text-xs text-ink-3 break-words">{s.detail}</p>
                      )}

                      {/* 质检逐次尝试：哪一次、多少分、因为什么被打回 */}
                      {s.id === 'qc' && s.items.length > 0 && (
                        <ul className="mt-1.5 space-y-1">
                          {s.items.map((it, j) => (
                            <li
                              key={j}
                              className="rounded-md border border-line bg-surface-2 px-2 py-1.5 text-xs"
                            >
                              <div className="flex items-center gap-2">
                                <span className="text-ink-2">
                                  {t('precip.qc.attempt', { n: it.attempt ?? j + 1 })}
                                </span>
                                {it.score != null && (
                                  <span className="text-ink-3">
                                    {t('precip.qc.score', { score: it.score })}
                                  </span>
                                )}
                                <span className={it.passed ? 'text-success-strong' : 'text-danger-strong'}>
                                  {it.passed ? t('precip.qc.passed') : t('precip.qc.failed')}
                                </span>
                                {it.seed != null && (
                                  <span className="ml-auto text-ink-3">seed {it.seed}</span>
                                )}
                              </div>
                              {it.reason && (
                                <p className="mt-0.5 text-ink-3 break-words">{it.reason}</p>
                              )}
                              {!!it.issues?.length && (
                                <ul className="mt-0.5 list-disc pl-4 text-ink-3">
                                  {it.issues.map((x, k) => (
                                    <li key={k} className="break-words">{x}</li>
                                  ))}
                                </ul>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}

                      {/* 教训：下次重画时会被召回用于改写提示词 */}
                      {s.id === 'lesson' && s.items.length > 0 && (
                        <ul className="mt-1.5 space-y-1">
                          {s.items.map((it, j) => (
                            <li
                              key={j}
                              className="rounded-md border border-line bg-surface-2 px-2 py-1.5 text-xs"
                            >
                              <div className="flex items-start gap-2">
                                <span className="min-w-0 flex-1 text-ink-2 break-words">{it.issue}</span>
                                {it.category && (
                                  <span className="shrink-0 rounded-full bg-surface px-1.5 py-0.5 text-xs text-ink-3">
                                    {it.category}
                                  </span>
                                )}
                                {it.priority && (
                                  <span className="shrink-0 rounded-full bg-surface px-1.5 py-0.5 text-xs text-ink-3">
                                    {it.priority}
                                  </span>
                                )}
                              </div>
                              {it.project && (
                                <p className="mt-0.5 text-ink-3">
                                  {t('precip.lesson.from', { project: it.project })}
                                </p>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}

                      {/* 通用键值条目（抽取字段 / 提示词 / seed / 视角清单 / 目录） */}
                      {s.id !== 'qc' && s.id !== 'lesson' && s.items.filter((x) => x.key).length > 0 && (
                        <dl className="mt-1 space-y-0.5">
                          {s.items.filter((x) => x.key).map((it, j) => (
                            <div key={j} className="flex gap-2 text-xs">
                              <dt className="shrink-0 text-ink-3">{it.key}</dt>
                              <dd className="min-w-0 flex-1 text-ink-2 break-words whitespace-pre-wrap">
                                {it.value}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ========== 服装变体（衣柜，2026-10-02） ==========
// 角色资产预览弹层里的小入口：展开显示已生成的服装变体列表
// （GET /api/assets/character/outfits），并提供「+ 新增服装变体」表单
// （服装名 + 服装描述 → POST /api/assets/character/outfit，后端复用资产生成
// 全链路，异步进度可看任务列表）。仅角色类型显示；任何接口异常都降级为空列表 /
// toast 提示，绝不打断预览主体（fail-open，与后端零回归约束对齐）。
function CharacterOutfitsSection({ projectKey, character }: { projectKey: string; character: string }) {
  const { t } = useApp();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [outfits, setOutfits] = useState<CharacterOutfit[]>([]);
  const [loading, setLoading] = useState(false);
  const [outfitKey, setOutfitKey] = useState('');
  const [outfitDesc, setOutfitDesc] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const d = await characterOutfits.list(projectKey, character);
      setOutfits(Array.isArray(d?.outfits) ? d.outfits : []);
    } catch {
      setOutfits([]);   // 目录不存在 / 读取失败 → 空列表，不打断预览
    } finally {
      setLoading(false);
    }
  }, [projectKey, character]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const submit = async () => {
    if (submitting) return;
    if (!outfitKey.trim() || !outfitDesc.trim()) {
      toast.warning(t('assets.outfit.required'));
      return;
    }
    setSubmitting(true);
    try {
      const d = await characterOutfits.generate({
        project_name: projectKey,
        character,
        outfit_key: outfitKey.trim(),
        outfit_desc: outfitDesc.trim(),
      });
      if (d?.skipped) {
        toast.info(d.message || t('assets.outfit.skipped'));
      } else {
        toast.success(t('assets.outfit.started'));
      }
      setOutfitKey('');
      setOutfitDesc('');
      setOpen(true);
      void load();
    } catch (err: any) {
      toast.error(err?.message || t('assets.outfit.failed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="border border-line rounded-lg p-3 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`flex w-full items-center justify-between rounded text-sm font-semibold text-ink-1 hover:text-brand transition-colors ${FOCUS_RING}`}
      >
        <span>{t('assets.outfit.title')}</span>
        <span className="text-xs text-ink-3">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="space-y-2">
          {loading ? (
            <div className="text-xs text-ink-3">{t('common.loading')}</div>
          ) : outfits.length === 0 ? (
            <div className="text-xs text-ink-3">{t('assets.outfit.empty')}</div>
          ) : (
            <ul className="space-y-1.5">
              {outfits.map((o) => (
                <li key={o.outfit_key}
                    className="flex items-center justify-between gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-1.5">
                  <span className="min-w-0">
                    <span className="block truncate text-sm text-ink-1">{o.outfit_key}</span>
                    {o.desc && (
                      <span className="block truncate text-xs text-ink-3">{o.desc}</span>
                    )}
                  </span>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${
                      o.ready ? 'bg-success-subtle text-success-strong' : 'bg-surface-2 text-ink-3'
                    }`}
                  >
                    {o.ready ? t('assets.outfit.ready') : t('assets.outfit.generating')}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {/* 新增服装变体：服装名 + 服装描述（⚠️ ui.Input 的 onChange 直接传 string 值） */}
          <div className="space-y-1.5 pt-1">
            <Input
              value={outfitKey}
              onChange={(v) => setOutfitKey(v)}
              placeholder={t('assets.outfit.keyPlaceholder')}
            />
            <Input
              value={outfitDesc}
              onChange={(v) => setOutfitDesc(v)}
              placeholder={t('assets.outfit.descPlaceholder')}
            />
            <Button size="sm" variant="secondary" onClick={submit} loading={submitting}>
              {t('assets.outfit.add')}
            </Button>
          </div>
          <p className="text-xs text-ink-3">{t('assets.outfit.hint')}</p>
        </div>
      )}
    </div>
  );
}

// ========== QC Tab（功能质检） ==========
// 后端 /api/qc/project-summary 早已返回「引擎状态 + 统计 + 逐镜质检明细」，
// 但前端此前只有标签没有渲染 —— 点进去是空白。这里补齐只读总览 + 单镜重测。
function QcTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const toast = useToast();
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [testing, setTesting] = useState<string | null>(null);
  // D2（2026-09-23）：质检配置**前端零入口** —— qcApi 的 updateConfig / clearConfig /
  // resetEndpoint / syncFromAI 此前在这页一个都没被调用（全站只有 AudioTab 调
  // updateConfig 改音频阈值），QcTab 是纯只读看板。而 qc_client._empty_config() 的
  // enabled 默认是 **False** → 新环境部署后质检**静默全关**，用户只看到「未启用」
  // 却找不到任何开关。这里补齐：总开关 / 分品类开关 / 合格线与重试 / 参考图对照 /
  // 端点三态动作。
  // 草稿 cfgDraft 与展示用的 data.config 分离：避免「一边编辑一边被 load() 覆盖」，
  // 「单镜重测」触发的重载也不会冲掉正在编辑的内容。
  const [cfgDraft, setCfgDraft] = useState<any>(null);
  const [cfgSaving, setCfgSaving] = useState(false);
  const [cfgBusy, setCfgBusy] = useState('');
  const [confirmClearOpen, setConfirmClearOpen] = useState(false);

  // refreshDraft=true：用服务端配置重建草稿（首次加载 / 手动刷新 / 保存与端点动作之后）
  const load = async (refreshDraft = false) => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      const resp: any = await qcApi.history(projectKey);
      setData(resp);
      const next = resp?.config ? { ...resp.config } : null;
      setCfgDraft((prev: any) => (refreshDraft || !prev ? next : prev));
    } catch (e) {
      setError(sanitizeError(e, t('qc.loadFailed')));
    } finally {
      setLoading(false);
    }
  };

  // 切项目必须丢弃上一项目的草稿（否则会把 A 项目的配置保存到 B 项目）
  useEffect(() => { setCfgDraft(null); void load(true); }, [projectKey]);

  const runTest = async (shotId: string) => {
    setTesting(shotId);
    setError('');
    setNotice('');
    try {
      const r = await qcApi.test({ project: projectKey, shot_id: shotId });
      const verdict = r.verdict === 'pass' ? t('qc.passed') : r.verdict === 'fail' ? t('qc.notPassed') : String(r.verdict || t('qc.unknownVerdict'));
      setNotice(`${t('qc.retestDone', { verdict })}${typeof r.score === 'number' ? t('qc.retestScore', { n: Math.round(r.score * 100) }) : ''}`);
      await load();
    } catch (e) {
      setError(sanitizeError(e, t('qc.retestFailed')));
    } finally {
      setTesting(null);
    }
  };

  // ---- D2：质检配置写操作 ----
  // ⚠️ 只提交本面板管辖的字段：save_config 按 CONFIG_KEYS 白名单**合并**，未提交的键保持
  // 不动 —— 这样 AudioTab 改过的音频阈值、以及 prompt 类配置不会被这里的草稿覆盖。
  const saveCfg = async () => {
    if (!cfgDraft) return;
    setCfgSaving(true);
    setError('');
    setNotice('');
    try {
      const resp: any = await qcApi.updateConfig({
        enabled: !!cfgDraft.enabled,
        script_enabled: !!cfgDraft.script_enabled,
        image_enabled: !!cfgDraft.image_enabled,
        video_enabled: !!cfgDraft.video_enabled,
        audio_enabled: !!cfgDraft.audio_enabled,
        keyframe_qc_enabled: !!cfgDraft.keyframe_qc_enabled,
        image_ref_compare: !!cfgDraft.image_ref_compare,
        pass_score: Number(cfgDraft.pass_score),
        max_retries: Number(cfgDraft.max_retries),
        best_of: Number(cfgDraft.best_of),
        video_frame_count: Number(cfgDraft.video_frame_count),
        timeout: Number(cfgDraft.timeout),
      } as any);
      // 后端在「总开关开了、但接口信息不全」时会回 warning：此时生成流程会**静默跳过**
      // 质检，必须原样透出给用户，否则又是一个「以为在质检其实没检」。
      setNotice(t('qc.cfgSaved') + (resp?.warning ? `；⚠️ ${resp.warning}` : ''));
      toast.success(t('qc.cfgSaved'));
      await load(true);
    } catch (e) {
      const msg = sanitizeError(e, t('qc.cfgSaveFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setCfgSaving(false);
    }
  };

  const doSyncFromAi = async () => {
    setCfgBusy('sync');
    setError('');
    setNotice('');
    try {
      await qcApi.syncFromAI();
      setNotice(t('qc.syncDone'));
      toast.success(t('qc.syncDoneToast'));
      await load(true);
    } catch (e) {
      const msg = sanitizeError(e, t('qc.syncFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setCfgBusy('');
    }
  };

  const doResetEndpoint = async () => {
    setCfgBusy('reset');
    setError('');
    setNotice('');
    try {
      await qcApi.resetEndpoint();
      setNotice(t('qc.resetDone'));
      toast.success(t('qc.resetDoneToast'));
      await load(true);
    } catch (e) {
      const msg = sanitizeError(e, t('qc.resetFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setCfgBusy('');
    }
  };

  const doClearCfg = async () => {
    setCfgBusy('clear');
    setError('');
    setNotice('');
    try {
      await qcApi.clearConfig();
      setConfirmClearOpen(false);
      setNotice(t('qc.clearDone'));
      toast.success(t('qc.cleared'));
      await load(true);
    } catch (e) {
      const msg = sanitizeError(e, t('qc.clearFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setCfgBusy('');
    }
  };

  const verdictBadge = (v: string) => {
    if (v === 'pass') return 'bg-success-subtle text-success-strong';
    if (v === 'fail') return 'bg-danger-subtle text-danger-strong';
    return 'bg-surface-2 text-ink-1';
  };

  if (loading) return (
    // 骨架对齐真实区块：标题行 → 质检引擎卡 → 4 张统计卡 → 逐镜明细卡
    <div className="space-y-6" role="status" aria-live="polite" aria-label={t('common.loading')}>
      <div className="flex justify-between items-center">
        <Skeleton className="h-5 w-24" />
        <Skeleton className="h-8 w-16" />
      </div>
      <Skeleton className="h-40 rounded-lg" />
      <div className="grid grid-cols-4 gap-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-20 rounded-lg" />
        ))}
      </div>
      <Skeleton className="h-48 rounded-lg" />
    </div>
  );

  // 硬失败：质检数据整体没取到（data 仍为空），下面的引擎状态与统计只会渲染成空壳
  if (error && !data) {
    return (
      <ErrorState
        title={t('project.loadingFailed')}
        description={error}
        onRetry={load}
      />
    );
  }

  const cfg = data?.config || {};
  const stats = data?.stats || { total: 0, passed: 0, failed: 0, retry_count: 0 };
  const records: any[] = Array.isArray(data?.history) ? data.history : [];
  // 后端 history[].kind 是**质检品类**（图片/视频/尾帧/资产/音频/剧本/提示词），
  // 此前一律渲染成「图像」，资产与提示词的记录显示得驴唇不对马嘴。
  const KIND_LABEL: Record<string, string> = {
    video: t('qc.kind.video'), image: t('qc.kind.image'), keyframe: t('qc.kind.keyframe'), asset: t('qc.kind.asset'),
    audio: t('qc.kind.audio'), script: t('qc.kind.script'), prompt: t('qc.kind.prompt'),
  };

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h3 className="text-lg font-semibold text-ink-1">{t('qc.heading')}</h3>
        <Button size="sm" variant="secondary" onClick={load}>{t('common.refresh')}</Button>
      </div>

      {notice && (
        <div className="p-3 bg-success-subtle border border-success/30 rounded-lg text-success-strong text-sm">
          {notice}
        </div>
      )}
      {error && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">{error}</div>
      )}

      {/* 质检引擎状态 */}
      <div className="bg-surface rounded-lg border border-line p-4">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-ink-1">{t('qc.engine')}</span>
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
            cfg.enabled ? 'bg-success-subtle text-success-strong'
                        : 'bg-surface-2 text-ink-2'
          }`}>
            {cfg.enabled ? t('qc.enabledOn') : t('qc.enabledOff')}
          </span>
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
            cfg.ready ? 'bg-brand-subtle text-brand-hover'
                      : 'bg-warning-subtle text-warning-strong'
          }`}>
            {cfg.ready ? t('qc.ready') : t('qc.notReady')}
          </span>
        </div>
        <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div>
            <div className="text-ink-2 text-xs">{t('qc.model')}</div>
            <div className="text-ink-1 truncate">{cfg.effective_model || cfg.model || '—'}</div>
          </div>
          <div>
            <div className="text-ink-2 text-xs">{t('qc.passLine')}</div>
            <div className="text-ink-1">{cfg.pass_score ?? '—'}</div>
          </div>
          <div>
            <div className="text-ink-2 text-xs">{t('qc.endpoint')}</div>
            <div className="text-ink-1 truncate">{cfg.effective_base_url || cfg.base_url || '—'}</div>
          </div>
          <div>
            <div className="text-ink-2 text-xs">API Key</div>
            <div className="text-ink-1">{cfg.has_api_key ? (cfg.api_key_masked || t('qc.configured')) : t('qc.notConfigured')}</div>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          {/* 这里显示的是**实际是否生效**（*_qc_active），不是「用户配了什么」——
              配置开关在下方「质检配置」里，两处刻意分工：
              总开关关 / 品类开关关 / 接口没配全，都会让某个品类「未生效」。 */}
          {[
            { label: t('qc.kind.script'), on: cfg.script_qc_active },
            { label: t('qc.kind.image'), on: cfg.image_qc_active },
            { label: t('qc.kind.video'), on: cfg.video_qc_active },
            { label: t('qc.kind.audio'), on: cfg.audio_qc_active },
          ].map((k) => (
            <span key={k.label} className={`px-2 py-0.5 rounded ${
              k.on ? 'bg-brand-subtle text-brand'
                   : 'bg-surface-2 text-ink-2'
            }`}>
              {k.label}{t('qc.suffix')} {k.on ? t('qc.active') : t('qc.inactive')}
            </span>
          ))}
        </div>
      </div>

      {/* 质检配置（D2 2026-09-23：此前前端零入口，enabled 默认 false → 新环境质检静默全关） */}
      {cfgDraft && (
        <div className="bg-surface rounded-lg border border-line p-4">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <span className="font-medium text-ink-1">{t('qc.config')}</span>
            <span className="text-xs text-ink-3">
              {t('qc.sourcePrefix')}：{cfg.endpoint_auto_synced ? t('qc.sourceAuto') : t('qc.sourceManual')}
            </span>
          </div>

          <label className="mt-3 flex flex-wrap items-center gap-2 text-sm text-ink-1">
            {/* 保留原生：共享组件未覆盖 checkbox */}
            <input
              type="checkbox"
              checked={!!cfgDraft.enabled}
              onChange={(e) => setCfgDraft({ ...cfgDraft, enabled: e.target.checked })}
              className={`rounded ${FOCUS_RING}`}
            />
            {t('qc.enableMaster')}
            <span className="text-xs text-ink-3">{t('qc.enableMasterHint')}</span>
          </label>

          <div className="mt-3 grid grid-cols-2 md:grid-cols-5 gap-2">
            {[
              { key: 'script_enabled', label: t('qc.kind.script') },
              { key: 'image_enabled', label: t('qc.kind.image') },
              { key: 'video_enabled', label: t('qc.kind.video') },
              { key: 'audio_enabled', label: t('qc.kind.audio') },
              { key: 'keyframe_qc_enabled', label: t('qc.kind.keyframe') },
            ].map((k) => (
              <label key={k.key} className="flex items-center gap-2 text-sm text-ink-2">
                <input
                  type="checkbox"
                  checked={!!cfgDraft[k.key]}
                  onChange={(e) => setCfgDraft({ ...cfgDraft, [k.key]: e.target.checked })}
                  className={`rounded ${FOCUS_RING}`}
                />
                {k.label}{t('qc.suffix')}
              </label>
            ))}
          </div>

          <label className="mt-2 flex flex-wrap items-center gap-2 text-sm text-ink-2">
            <input
              type="checkbox"
              checked={!!cfgDraft.image_ref_compare}
              onChange={(e) => setCfgDraft({ ...cfgDraft, image_ref_compare: e.target.checked })}
              className={`rounded ${FOCUS_RING}`}
            />
            {t('qc.refCompare')}
            <span className="text-xs text-ink-3">
              {t('qc.refCompareHint')}
            </span>
          </label>

          {/* 保留原生：这几个 number 输入带 step/min/max 约束与数值型默认值，
              Input 组件未开放 step/min/max，换成 Input 会静默丢掉步进与取值范围 */}
          <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.passScoreLabel')}</div>
              <input
                type="number" step="1" min="0" max="100"
                value={cfgDraft.pass_score ?? 70}
                onChange={(e) => setCfgDraft({ ...cfgDraft, pass_score: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.maxRetriesLabel')}</div>
              <input
                type="number" step="1" min="0" max="5"
                value={cfgDraft.max_retries ?? 2}
                onChange={(e) => setCfgDraft({ ...cfgDraft, max_retries: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.bestOfLabel')}</div>
              <input
                type="number" step="1" min="1" max="4"
                value={cfgDraft.best_of ?? 1}
                onChange={(e) => setCfgDraft({ ...cfgDraft, best_of: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
              <div className="text-xs text-ink-3 mt-1">{t('qc.bestOfHint')}</div>
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.videoFramesLabel')}</div>
              <input
                type="number" step="1" min="1" max="6"
                value={cfgDraft.video_frame_count ?? 3}
                onChange={(e) => setCfgDraft({ ...cfgDraft, video_frame_count: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('qc.timeoutLabel')}</div>
              <input
                type="number" step="10" min="30" max="600"
                value={cfgDraft.timeout ?? 180}
                onChange={(e) => setCfgDraft({ ...cfgDraft, timeout: e.target.value })}
                className={`w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 ${FOCUS_RING}`}
              />
            </div>
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button onClick={saveCfg} loading={cfgSaving} disabled={cfgSaving} className="text-sm">
              {t('qc.saveCfg')}
            </Button>
            <Button
              variant="secondary"
              onClick={doSyncFromAi}
              disabled={cfgBusy !== ''}
              className="text-sm"
            >
              {cfgBusy === 'sync' ? t('qc.syncing') : t('qc.syncFromAi')}
            </Button>
            <Button
              variant="secondary"
              onClick={doResetEndpoint}
              disabled={cfgBusy !== ''}
              className="text-sm"
            >
              {cfgBusy === 'reset' ? t('qc.resetting') : t('qc.resetToAi')}
            </Button>
            <Button
              variant="danger"
              onClick={() => setConfirmClearOpen(true)}
              disabled={cfgBusy !== ''}
              className="text-sm"
            >
              {t('qc.clearCfg')}
            </Button>
          </div>
          <p className="mt-2 text-xs text-ink-3">
            {t('qc.helpText')}
          </p>
        </div>
      )}

      <Modal
        isOpen={confirmClearOpen}
        onClose={() => setConfirmClearOpen(false)}
        title={t('qc.clearCfg')}
        closeOnBackdrop={false}
        closeOnEsc={!cfgBusy}
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => setConfirmClearOpen(false)}
              disabled={!!cfgBusy}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="danger"
              onClick={doClearCfg}
              loading={cfgBusy === 'clear'}
              disabled={!!cfgBusy}
            >
              {t('qc.confirmClear')}
            </Button>
          </>
        }
      >
        <p className="text-sm text-ink-2">
          {t('qc.clearBody1')}
          <strong className="text-ink-1">{t('qc.clearBodyStrong')}</strong>{t('qc.clearBody2')}
          {t('qc.clearBody3')}<strong className="text-ink-1">{t('qc.clearBodyStrong2')}</strong>
        </p>
        <p className="mt-2 text-sm text-ink-2">
          {t('qc.clearHint')}
        </p>
      </Modal>

      {/* 统计 */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: t('qc.totalStats'), value: stats.total, color: 'text-ink-1' },
          { label: t('qc.passed'), value: stats.passed, color: 'text-success-strong' },
          { label: t('qc.notPassed'), value: stats.failed, color: 'text-danger-strong' },
          { label: t('qc.pending'), value: stats.retry_count, color: 'text-warning-strong' },
        ].map((s) => (
          <div key={s.label} className="bg-surface rounded-lg border border-line p-4 text-center">
            <div className={`text-2xl font-bold ${s.color}`}>{s.value ?? 0}</div>
            <div className="text-xs text-ink-2 mt-1">{s.label}</div>
          </div>
        ))}
      </div>

      {/* 逐镜明细（P2-15：改为**语义化表格**）—— 此前是 div 模拟的六列「表格」，
          读屏只能听到一串无结构的文本，既读不出行列关系，也没有表头关联。 */}
      {records.length === 0 ? (
        <EmptyState
          icon={<ClipboardCheck className="h-10 w-10" />}
          title={t('qc.noRecords')}
          description={t('qc.noRecordsHint')}
        />
      ) : (
        <div className="bg-surface rounded-lg border border-line overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">{t('qc.detailCaption')}</caption>
            <thead>
              <tr className="border-b border-line text-xs text-ink-2">
                <th scope="col" className="text-left font-medium p-3">{t('common.shot')}</th>
                <th scope="col" className="text-left font-medium p-3">{t('qc.kindCol')}</th>
                <th scope="col" className="text-left font-medium p-3">{t('qc.verdict')}</th>
                <th scope="col" className="text-left font-medium p-3">{t('common.score')}</th>
                <th scope="col" className="text-left font-medium p-3">{t('qc.time')}</th>
                <th scope="col" className="text-right font-medium p-3">{t('qc.actions')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {records.map((r, i) => (
                <tr key={`${r.shot_id}-${r.kind}-${i}`}>
                  <th scope="row" className="text-left font-mono font-normal text-ink-1 p-3 align-middle">
                    {r.shot_id}
                  </th>
                  <td className="p-3 align-middle">
                    <span className="text-xs px-2 py-0.5 rounded bg-surface-2 text-ink-2">
                      {KIND_LABEL[r.kind] || r.kind || t('wb.qc')}
                    </span>
                  </td>
                  <td className="p-3 align-middle">
                    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${verdictBadge(r.verdict)}`}>
                      {r.verdict === 'pass' ? t('qc.passed') : r.verdict === 'fail' ? t('qc.notPassed')
                        : r.verdict === 'error' ? t('qc.errorVerdict') : r.verdict === 'unknown' ? t('qc.pending')
                        : String(r.verdict || t('common.unknown'))}
                    </span>
                  </td>
                  <td className="p-3 align-middle text-ink-2">
                    {/* ⚠️ 后端 score 是 **0~100**（实测区间 15~98），不是 0~1 的比例。
                        这里此前无条件 *100，会把 82 分显示成「8200」。 */}
                    {typeof r.score === 'number'
                      ? Math.round(r.score <= 1 ? r.score * 100 : r.score)
                      : '—'}
                  </td>
                  <td className="p-3 align-middle text-xs text-ink-3 whitespace-nowrap">
                    {r.timestamp ? String(r.timestamp).replace('T', ' ').slice(0, 19) : '—'}
                  </td>
                  <td className="p-3 align-middle text-right">
                    {(r.kind === 'image' || r.kind === 'video') && (
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={testing === r.shot_id}
                        onClick={() => runTest(r.shot_id)}
                      >
                        {testing === r.shot_id ? t('qc.retesting') : t('qc.retest')}
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ========== 分镜管理（关键帧 + 分镜序列） ==========
// 2026-10-06：九宫格草案子页已下线（后端 /storyboard/nine-grid 已删，候选构图入口
// 移到分镜卡片上的「九宫格候选构图」按钮），只保留 分镜序列 / 关键帧 两个子标签。
//
// 拆成 2 个顶级标签会让用户在标签间来回跳，这里收成一个标签页 + 2 个子标签。
//
// ⭐ 集级隔离（2026-09-25）：同一部小说会产多集，而分镜图/尾帧/视频**按集落盘**
// （第 1 集平铺，第 2 集起 `epNN/`）。此前这里**完全不选集** ——
// `storyboardApi.canvas(projectKey)` 不带 episode_no，后端 `_load_script_for` 就
// 只看「已生成的集里最新的那集」，于是无论用户在哪一集，看到的永远是最近一集的分镜；
// 关键帧同理。这里统一加一个集切换器，把选中集号透传给下面两个子页。
function useEpisodeList(novelId?: string) {
  const [episodes, setEpisodes] = useState<any[]>([]);
  useEffect(() => {
    if (!novelId) {
      setEpisodes([]);
      return;
    }
    let alive = true;
    episodesApi.list(novelId)
      .then((d) => { if (alive) setEpisodes(d.episodes || []); })
      .catch(() => { if (alive) setEpisodes([]); });
    return () => { alive = false; };
  }, [novelId]);
  return episodes;
}

function EpisodeSwitcher({
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


// =====================================================================
// 分镜提示词编辑器（2026-09-30）
// 每行 shot 的 description + motion 可编辑，保存调 PUT /api/episodes/...
// 保存成功后可触发单镜重新生成分镜图
// =====================================================================
function ShotPromptEditor({ shot, novelId, episodeNo, onSaved }: {
  shot: any; novelId?: string; episodeNo: number; onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [desc, setDesc] = useState(shot.description || '');
  const [motion, setMotion] = useState(shot.motion || '');
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const toast = useToast();

  // G10b（2026-09-30）：质检改写的提示词**实时渲染** —— 出图/重试循环每改写一次 prompt，
  // 后端任务状态里的 live 字段就更新一次；本组件启动「重新生成分镜」后每 3s 轮询一次，
  // 把「当前正在用的提示词」渲染到本镜头卡片下（不再等任务收尾才可见）。
  const [live, setLive] = useState<any>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; stopPolling(); }, []);

  const stopPolling = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  };

  const pollTask = (taskId: string) => {
    stopPolling();
    const tick = async () => {
      if (!mountedRef.current) return stopPolling();
      try {
        const r = await fetch(`/api/generation/status/${encodeURIComponent(taskId)}`);
        const d: any = await r.json();
        if (!mountedRef.current) return stopPolling();
        const lv = d?.live;
        if (lv && lv.shot === shot.shot_id && lv.prompt) {
          setLive(lv);
        }
        if (d?.status && d.status !== 'running') {
          // 任务结束：若最后一帧 live 还没标记定稿，补上；停止轮询；刷新剧本数据
          setLive((prev: any) => (prev && prev.shot === shot.shot_id && !prev.done
            ? { ...prev, done: true } : prev));
          stopPolling();
          onSaved();
        }
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
      if (!d.success) throw new Error(d.error || '保存失败');
      toast.success(`镜头 ${shot.shot_id} 提示词已保存`);
      onSaved();
    } catch (e: any) {
      toast.error(e.message || '保存失败');
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
        toast.info(`分镜 ${shot.shot_id} 重新生成任务已启动（提示词随质检实时刷新）`);
      } else {
        throw new Error(d.error || '启动失败');
      }
    } catch (e: any) {
      toast.error(e.message || '重新生成失败');
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
            ? '定稿提示词（本轮出图实际使用）'
            : `QC 实时改写 · 第 ${(live.attempt ?? 0) + 1} 次尝试 · ${live.phase === 'regenerating' ? '改写后重新生成中' : live.phase === 'checking' ? '质检判定中' : '生成中'}`}
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
            编辑
          </button>
        </div>
        {shot.motion && <p className="text-xs text-ink-3 ml-2">{shot.motion}</p>}
        {livePanel}
      </div>
    );
  }

  return (
    <div className="mt-2 bg-surface rounded border border-line p-3 space-y-2">
      <label className="text-xs font-medium text-ink-2 block">镜头描述（提示词）</label>
      <textarea
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        rows={2}
        className="w-full text-sm bg-surface border border-line rounded p-2 text-ink-1 resize-y"
        placeholder="中近景（腰部以上取景），赵天霸右手食指伸出指向右前方…"
      />
      <label className="text-xs font-medium text-ink-2 block">画面内动作（motion）</label>
      <textarea
        value={motion}
        onChange={(e) => setMotion(e.target.value)}
        rows={2}
        className="w-full text-sm bg-surface border border-line rounded p-2 text-ink-1 resize-y"
        placeholder="【摄影机】无；【画面内】赵天霸右手食指前伸，左手丹丸位于胸前"
      />
      {livePanel}
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={save} disabled={saving || !desc.trim()}>
          {saving ? '保存中…' : '保存提示词'}
        </Button>
        <Button size="sm" variant="secondary" onClick={regenerate} disabled={regenerating || !desc.trim()}>
          {regenerating ? '生成中…' : '重新生成分镜'}
        </Button>
        <button className="text-xs text-ink-3 hover:text-ink-1 cursor-pointer" onClick={() => setEditing(false)}>取消</button>
      </div>
    </div>
  );
}

function StoryboardHubTab({ projectKey, novelId }: { projectKey: string; novelId?: string }) {
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

  return (
    <div className="space-y-5">
      {/* 集切换器：分镜产物按集隔离，必须先在集之间分流 */}
      <EpisodeSwitcher
        episodes={episodes}
        value={selectedEpisode}
        onChange={setSelectedEpisode}
      />

      <StoryboardTab projectKey={projectKey} episodeNo={selectedEpisode} />
    </div>
  );
}

// ========== Storyboard Tab ==========
// 单镜重做闭环：后端 /api/storyboard/retry-shot（分镜图）与 /api/video/retry-shot（视频）
// 早已实现，但前端此前**零入口** —— 用户对某一镜不满意只能整集重跑。
// 这里把两个入口放到每张分镜卡上，并在视频重做成功后提示「同集成片已过期」。
// 项目级视频生成方式（写入 config.video_mode）。
// ⚠️ 2026-10-02 修复：后端 `config.norm_video_mode` 已**只保留「整集一次生成」**
//   （per_shot / keyframe 两种模式废弃，任何入口一律归一成 episode，见 config.py:384）。
//   这里原先仍列 3 项并注释「取值与后端 config.VIDEO_MODES 一致」——**注释与事实不符**
//   （自我背书的错误注释），用户选 per_shot/keyframe 会被后端**静默归一**，
//   界面却仍显示所选值，属静默降级。现与后端同源收敛为单值。
//   注意与「每镜重做模式」（reference / keyframe，只管这一镜怎么重做）是两件事。
const PROJECT_VIDEO_MODE_OPTIONS: { value: VideoMode; labelKey: string }[] = [
  { value: 'episode', labelKey: 'project.videoModeEpisode' },
];

// 批量重生成单次上限：与后端 /api/video/retry-shots-batch 的硬上限（≤12）一致
const BATCH_RETRY_LIMIT = 12;

function StoryboardTab({ projectKey, episodeNo }: { projectKey: string; episodeNo?: number | null }) {
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

  // ---- 分镜九宫格候选构图（P2-1：grid-candidates 生成 3x3 候选 → 点选某格 → grid-apply 裁切入库）----
  /** 正在操作九宫格的镜头（sid + 展示用序号）；null = 弹窗关闭 */
  const [gridTarget, setGridTarget] = useState<{ sid: string; seq: number } | null>(null);
  /** 弹窗内阶段：generating=候选图生成中 / ready=可点选（或已失败可重试）/ applying=裁切入库中 */
  const [gridPhase, setGridPhase] = useState<'generating' | 'ready' | 'applying'>('generating');
  /** 后端生成的 3x3 候选网格图地址（generation 任务完成态的 grid_url） */
  const [gridImgUrl, setGridImgUrl] = useState('');
  /** 用户点选的格号（1-9，行优先：1=左上 … 9=右下，与后端 crop_grid_cell 的等分切分一致） */
  const [gridCell, setGridCell] = useState<number | null>(null);
  /** 候选生成失败原因（弹窗内展示，可原地重试） */
  const [gridError, setGridError] = useState('');
  /** 九宫格轮询句柄（弹窗关闭 / 换镜 / 卸载时必须清掉，否则会一直打后端） */
  const gridPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  /** 进行中的候选生成任务（sid → task_id）：弹窗被提前关掉后重开可续接轮询，不重复烧卡 */
  const gridTaskRef = useRef<{ sid: string; taskId: string } | null>(null);

  // ---- 分镜图点击放大（lightbox）----
  /** 正在放大的分镜图：正式图或生成中 scratch 图；null = 弹窗关闭。
   *  分镜缩略图原先没有任何点击交互（点了没反应），这里补上全屏放大查看。 */
  const [zoomImg, setZoomImg] = useState<{ src: string; seq: number; grid?: boolean } | null>(null);

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

  const stopCanvasPoll = () => {
    if (canvasPollRef.current) { clearInterval(canvasPollRef.current); canvasPollRef.current = null; }
  };

  const fetchCanvas = async (opts?: { silent?: boolean }) => {
    if (!projectKey) return;
    const silent = !!opts?.silent;
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
      const msg = err instanceof Error ? err.message : t('sb.fetchFailed');
      if (msg.includes('404')) {
        setError('no-data');
      } else {
        setError(msg);
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
      setShotError(e instanceof Error ? e.message : t('sb.imageRedoFailed'));
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
    try {
      const r = await videoApi.retryShot({
        project_name: projectKey,
        shot_id: card.shot_id,
        mode,
        episode_no: episodeNo ?? undefined,
      });
      setNotice(
        t('sb.videoRedone', {
          seq: card.seq,
          mode: r.mode === 'keyframe' ? t('sb.modeKeyframe') : t('sb.modeReference'),
          refCount: r.ref_count,
          duration: r.duration,
        }) + (r.deliverable_marked_stale ? t('sb.videoRedoneStale') : '')
      );
      await fetchCanvas();
    } catch (e) {
      setShotError(e instanceof Error ? e.message : t('sb.videoRedoFailed'));
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
      toast.error(e instanceof Error ? e.message : t('sb.videoModeSaveFailed'));
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
      setShotError(e instanceof Error ? e.message : t('sb.videoRedoFailed'));
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
    try {
      const r = await videoApi.retryShotsBatch(projectKey, episodeNo ?? undefined, selectedShots);
      const results = Array.isArray(r.results) ? r.results : [];
      const ok = typeof r.ok_count === 'number'
        ? r.ok_count
        : results.filter((x) => x.success).length;
      const failures = results
        .filter((x) => !x.success)
        .map((x) => ({ shot_id: String(x.shot_id), error: x.error || t('video.batchRetry.unknownError') }));
      const total = results.length || selectedShots.length;
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

  // ---- 分镜九宫格候选构图（P2-1 前端入口） ----
  const stopGridPoll = () => {
    if (gridPollRef.current) { clearInterval(gridPollRef.current); gridPollRef.current = null; }
  };
  // 组件卸载时停掉轮询
  useEffect(() => () => stopGridPoll(), []);

  /** 关闭弹窗：停轮询并复位弹窗内状态（服务端任务继续跑；同镜重开时续接轮询，不重复发起） */
  const closeGridModal = () => {
    stopGridPoll();
    setGridTarget(null);
    setGridPhase('generating');
    setGridImgUrl('');
    setGridCell(null);
    setGridError('');
  };

  /** 轮询九宫格生成任务（3s）：完成取 grid_url 展示；失败/取消把原因留在弹窗内可重试。
   *  ⚠️ /api/generation/status 历史上存在 {success, task:{…}} 信封与顶层平铺两种返回形态，
   *     这里按 `task ?? 顶层` 兼容读取，不依赖其中一种。 */
  const pollGridTask = (taskId: string) => {
    stopGridPoll();
    const tick = async () => {
      try {
        const d = await generationApi.status(taskId) as unknown as ShotGridStatusResponse;
        const st: ShotGridTaskState = ((d as any)?.task ?? (d as any)) || {};
        if (st.status === 'completed') {
          stopGridPoll();
          gridTaskRef.current = null;
          setGridImgUrl(st.grid_url || '');
          if (!st.grid_url) setGridError(t('sb.grid.generateFailed'));
          setGridPhase('ready');
        } else if (st.status === 'failed' || st.status === 'cancelled') {
          stopGridPoll();
          gridTaskRef.current = null;
          setGridError(st.error || t('sb.grid.generateFailed'));
          setGridPhase('ready');
        }
      } catch { /* 网络抖动：等下一轮 */ }
    };
    tick();
    gridPollRef.current = setInterval(tick, 3000);
  };

  /** 发起九宫格候选生成（打开弹窗与弹窗内「重新生成」共用）：异步任务 + 轮询，防重复提交 */
  const runGridCandidates = async (target: { sid: string; seq: number }) => {
    setGridTarget(target);
    setGridPhase('generating');
    setGridImgUrl('');
    setGridCell(null);
    setGridError('');
    stopGridPoll();
    // 同一镜头已有候选任务在跑（上次弹窗被提前关掉）：直接续接轮询，不重复烧一次 GPU
    const running = gridTaskRef.current;
    if (running && running.sid === target.sid) {
      pollGridTask(running.taskId);
      return;
    }
    try {
      const r = await storyboardApi.gridCandidates(projectKey, episodeNo ?? 1, target.sid);
      gridTaskRef.current = { sid: target.sid, taskId: r.task_id };
      pollGridTask(r.task_id);
    } catch (e) {
      // 发起失败（镜号不存在 / 无可用参考图等 4xx）：留在弹窗内展示原因，可关闭或重试
      setGridError(e instanceof Error ? e.message : t('sb.grid.generateFailed'));
      setGridPhase('ready');
    }
  };

  /** 应用所选格：后端把该格从九宫格图裁切为该镜正式分镜图（人工定稿），成功后刷新画布 */
  const handleGridApply = async () => {
    if (!gridTarget || gridCell == null || gridPhase === 'applying') return;
    setGridPhase('applying');
    try {
      const r = await storyboardApi.gridApply(projectKey, episodeNo ?? 1, gridTarget.sid, gridCell);
      toast.success(t('sb.grid.applied', { seq: gridTarget.seq, cell: r.cell ?? gridCell }));
      closeGridModal();
      await fetchCanvas();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('sb.grid.applyFailed'));
      setGridPhase('ready');
    }
  };

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

      {notice && (
        <div className="p-3 bg-success-subtle border border-success/30 rounded-lg text-success-strong text-sm">
          {notice}
        </div>
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
        />
      )}

      {/* 硬失败：分镜数据整体没取到且无卡片可展示 → ErrorState；
          已有卡片时的刷新失败 → 行内条（软失败） */}
      {error && error !== 'no-data' && cards.length === 0 && (
        <ErrorState
          title={t('project.loadingFailed')}
          description={error}
          onRetry={fetchCanvas}
        />
      )}
      {error && error !== 'no-data' && cards.length > 0 && (
        <div className="p-4 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong">{error}</div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards.map((card: any) => {
          const sid = String(card.shot_id);
          const imgBusy = busy === `${card.shot_id}:image`;
          const vidBusy = busy === `${card.shot_id}:video`;
          const mode = videoMode[sid] || 'reference';
          const isSelected = selectedShots.includes(sid);
          return (
            <div key={card.seq} className="bg-surface rounded-lg border border-line p-4 flex flex-col">
              <div className="flex items-center justify-between mb-2">
                <label className="flex items-center gap-1.5 cursor-pointer">
                  {/* 批量重生成勾选：已达上限时未勾选的复选框禁用（原生 checkbox，与 QcTab 一致） */}
                  <input
                    type="checkbox"
                    checked={isSelected}
                    disabled={batchRunning || (!isSelected && selectedShots.length >= BATCH_RETRY_LIMIT)}
                    onChange={() => toggleShotSelected(sid)}
                    aria-label={t('video.batchRetry.pickShot', { seq: card.seq })}
                    className={`rounded ${FOCUS_RING}`}
                  />
                  <span className="font-mono text-sm text-ink-2">#{card.seq}</span>
                </label>
                <span className="text-xs text-ink-2">{card.camera}</span>
              </div>

              <div className="flex gap-3 mb-2">
                {(() => {
                  // 有可放大查看的图（正式图优先，其次生成中 scratch）才让缩略框可点击
                  const zoomSrc = (card.storyboard?.exists && card.storyboard?.url)
                    || (card.storyboard?.generating && card.storyboard?.scratch_url);
                  return (
                <div
                  role={zoomSrc ? 'button' : undefined}
                  tabIndex={zoomSrc ? 0 : undefined}
                  aria-label={zoomSrc ? t('sb.zoomTitle') : undefined}
                  onClick={zoomSrc ? () => setZoomImg({ src: zoomSrc, seq: card.seq, grid: !!card.storyboard?.grid }) : undefined}
                  onKeyDown={(e) => {
                    if (zoomSrc && (e.key === 'Enter' || e.key === ' ')) {
                      e.preventDefault();
                      setZoomImg({ src: zoomSrc, seq: card.seq, grid: !!card.storyboard?.grid });
                    }
                  }}
                  className={`w-24 h-24 shrink-0 rounded bg-surface-2 border overflow-hidden flex items-center justify-center text-xs text-ink-3 ${
                    zoomSrc ? `cursor-zoom-in ${FOCUS_RING}` : ''
                  } ${
                    card.storyboard?.generating && !card.storyboard?.exists
                      ? 'border-brand/50'
                      : 'border-line'
                  }`}
                >
                  {card.storyboard?.exists && card.storyboard?.url ? (
                    <img src={card.storyboard.url} alt={t('sb.imageAlt', { seq: card.seq })} className="w-full h-full object-cover" />
                  ) : card.storyboard?.generating && card.storyboard?.scratch_url ? (
                    // 生成中：正式产物尚未落盘，但已有中间产物 → 显示实时缩略图 + 角标
                    // ⚠️ 中间产物可能被后续 try 覆盖 → 加时间戳查询参数绕过浏览器缓存
                    <div className="relative w-full h-full" title={t('sb.generatingHint')}>
                      <img
                        src={`${card.storyboard.scratch_url}${card.storyboard.scratch_url.includes('?') ? '&' : '?'}t=${Date.now()}`}
                        alt={t('sb.imageAlt', { seq: card.seq })}
                        className="w-full h-full object-cover"
                      />
                      <span className="absolute inset-x-0 bottom-0 bg-brand/85 text-white text-xs leading-4 text-center">
                        {t('sb.generating')}
                      </span>
                    </div>
                  ) : card.storyboard?.generating ? (
                    <span className="flex flex-col items-center gap-1 text-brand">
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
                      <span>{t('sb.generating')}</span>
                    </span>
                  ) : (
                    <span>{t('sb.noImage')}</span>
                  )}
                </div>
                  );
                })()}
                <div className="min-w-0 flex-1 text-xs space-y-1">
                  <p className="text-ink-1 line-clamp-3">{card.description}</p>
                  {card.dialogue_text && (
                    <p className="text-ink-2 italic line-clamp-2">{card.dialogue_text}</p>
                  )}
                  <p className={card.video?.exists ? 'text-success-strong' : 'text-warning-strong'}>
                    {t('sb.video')}：{card.video?.exists ? t('sb.generated') : t('sb.notGenerated')}
                  </p>
                  {card.consistency?.score != null && (
                    <p className="text-ink-2">{t('sb.consistency')}：{card.consistency.score}</p>
                  )}
                </div>
              </div>

              {/* 内嵌播放器：插在信息区之后、操作区（带 mt-auto）之前，
                  使按钮仍被压到卡底，且与卡片内 mb-2 的间距风格一致。
                  ⚠️ 不写死高度、不加 aspect-video —— 重跑/超分后分辨率会变，交给浏览器按元数据自适应。 */}
              {playingVideo === sid && card.video?.url && (
                <video src={card.video.url} controls className="w-full mt-2 rounded-lg bg-black" />
              )}

              <div className="flex flex-wrap items-center gap-2 mt-auto pt-2">
                {card.video?.exists && card.video?.url && (
                  <>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => setPlayingVideo(playingVideo === sid ? null : sid)}
                    >
                      {playingVideo === sid ? t('common.close') : t('common.play')}
                    </Button>
                    {/* 端点未设 as_attachment，只能靠 HTML download 属性触发下载（同源，有效） */}
                    <a
                      href={card.video.url}
                      download
                      className={`text-xs text-brand hover:text-brand rounded-sm ${FOCUS_RING}`}
                    >
                      {t('common.download')}
                    </a>
                  </>
                )}
                <select
                  value={mode}
                  onChange={(e) =>
                    setVideoMode((prev) => ({ ...prev, [sid]: e.target.value as 'reference' | 'keyframe' }))
                  }
                  className={`text-xs rounded border border-line bg-surface text-ink-1 px-1 py-1 ${FOCUS_RING}`}
                  title={t('sb.modeHint')}
                >
                  <option value="reference">{t('sb.modeReference')}</option>
                  <option value="keyframe">{t('sb.modeKeyframe')}</option>
                </select>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => handleRetryImage(card)}
                  disabled={!!busy}
                >
                  {imgBusy ? t('sb.redoing') : t('sb.redoImage')}
                </Button>
                <Button
                  size="sm"
                  onClick={() => handleRetryVideo(card)}
                  disabled={!!busy}
                >
                  {vidBusy ? t('sb.redoing') : t('sb.redoVideo')}
                </Button>
                {/* 九宫格候选构图：一次生成 3x3 候选，弹窗内点选某格裁切为该镜分镜图。
                    生成中全站九宫格按钮禁用（防重复提交），弹窗内有进度提示 */}
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => runGridCandidates({ sid, seq: card.seq })}
                  loading={gridTarget?.sid === sid && gridPhase === 'generating'}
                  disabled={!!busy || gridPhase === 'generating' || gridPhase === 'applying'}
                  title={t('sb.grid.buttonHint')}
                >
                  {t('sb.grid.button')}
                </Button>
              </div>
            </div>
          );
        })}
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

      {/* 九宫格候选构图弹窗：生成中显示进度提示；完成后在候选图上按 3x3 等分覆盖 9 个
          透明选格按钮（行优先 1-9，与后端 crop_grid_cell 的整图等分切分逐格对齐），
          点格 → 「应用所选格」即裁切为该镜正式分镜图并刷新画布 */}
      <Modal
        isOpen={gridTarget !== null}
        onClose={closeGridModal}
        preventClose={gridPhase === 'applying'}
        title={t('sb.grid.modalTitle', { seq: gridTarget?.seq ?? '' })}
        description={t('sb.grid.pickHint')}
        size="lg"
        footer={
          <>
            <span className="mr-auto text-xs text-ink-2">
              {gridCell != null ? t('sb.grid.pickedCell', { n: gridCell }) : ''}
            </span>
            <Button variant="secondary" onClick={closeGridModal} disabled={gridPhase === 'applying'}>
              {t('common.close')}
            </Button>
            <Button
              onClick={handleGridApply}
              disabled={gridPhase !== 'ready' || gridCell == null || !!gridError || !gridImgUrl}
              loading={gridPhase === 'applying'}
            >
              {gridPhase === 'applying' ? t('sb.grid.applying') : t('sb.grid.apply')}
            </Button>
          </>
        }
      >
        {/* 生成中：ComfyUI 出图可能耗时一两分钟，给明确进度文案，防用户以为卡死 */}
        {gridPhase === 'generating' && (
          <Loading size="md" label={t('sb.grid.generating')} />
        )}

        {/* 生成失败 / 取消：原因留在弹窗内，可原地重试（异常不打断页面） */}
        {gridPhase !== 'generating' && gridError && (
          <div className="space-y-3">
            <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm break-all">
              {gridError}
            </div>
            <Button
              variant="secondary"
              onClick={() => gridTarget && runGridCandidates(gridTarget)}
              disabled={gridPhase === 'applying'}
            >
              {t('sb.grid.regenerate')}
            </Button>
          </div>
        )}

        {/* 候选图 + 3x3 选格覆盖层：划分与后端裁切同口径（整图等分三行三列，行优先） */}
        {gridPhase !== 'generating' && !gridError && gridImgUrl && (
          <div className="relative select-none">
            <img
              src={gridImgUrl}
              alt={t('sb.grid.imageAlt', { seq: gridTarget?.seq ?? '' })}
              className="block w-full rounded-md border border-line bg-surface-2"
            />
            <div className="absolute inset-0 grid grid-cols-3 grid-rows-3">
              {Array.from({ length: 9 }, (_, i) => i + 1).map((n) => (
                <button
                  key={n}
                  type="button"
                  aria-label={t('sb.grid.cellN', { n })}
                  aria-pressed={gridCell === n}
                  title={t('sb.grid.cellN', { n })}
                  onClick={() => setGridCell(n)}
                  className={`relative cursor-pointer transition-colors ${FOCUS_RING} ${
                    gridCell === n
                      ? 'border-2 border-brand bg-brand/25'
                      : 'border border-transparent hover:border-brand/70 hover:bg-brand/10'
                  }`}
                >
                  <span
                    className={`absolute left-1 top-1 rounded px-1.5 py-0.5 text-xs font-medium ${
                      gridCell === n ? 'bg-brand text-white' : 'bg-slate-900/70 text-white'
                    }`}
                  >
                    {n}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
      </Modal>

      {/* 分镜图点击放大（lightbox）：正式图 / 生成中 scratch 图都支持；原尺寸展示，ESC / 点遮罩关闭 */}
      <Modal
        isOpen={zoomImg !== null}
        onClose={() => setZoomImg(null)}
        title={t('sb.zoomTitle')}
        description={zoomImg ? `#${zoomImg.seq}` : ''}
        size="full"
      >
        {zoomImg && (
          <div className="relative mx-auto w-fit select-none">
            <img
              src={zoomImg.src}
              alt={t('sb.imageAlt', { seq: zoomImg.seq })}
              className="max-w-full max-h-[78vh] rounded-lg border border-line bg-surface-2 object-contain"
            />
            {/* 九宫格 1-9 编号覆盖层（2026-10-06）：编号由前端叠加，不再让模型画进图
                （扩散模型写数字实测乱码）。划分与后端裁切口径一致：整图等分三行三列。 */}
            {zoomImg.grid && (
              <div className="pointer-events-none absolute inset-0 grid grid-cols-3 grid-rows-3">
                {Array.from({ length: 9 }, (_, i) => i + 1).map((n) => (
                  <span key={n} className="relative">
                    <span className="absolute left-1 top-1 rounded bg-slate-900/70 px-1.5 py-0.5 text-xs font-medium text-white">
                      {n}
                    </span>
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

// ========== 超分（FlashVSR） ==========
// 接口：
// GET /api/upscale/env 链路自检（ComfyUI 在线 / 模型 / 节点）
// GET /api/upscale/sources 候选输入视频（成片 / 片段 / 已有超分 / ComfyUI）
// POST /api/upscale/video {project_name, video_path, scale, attach_audio} -> task_id
// GET /api/upscale/status/<id> 轮询进度
// GET /api/upscale/list 该项目已生成的超分产物
//
// ⚠️ attach_audio 必须传 true：后端 TE-Speed 链路默认 attach_audio=False，
// 对「成片」超分会把已合成的 TTS 配音整轨丢掉（backend 侧该参数此前也不在白名单，
// 已一并补上）。
//
// ⚠️ 可下载性取决于 URL 前缀：只有 /api/upscale/<project>/<name> 支持 ?download=1；
// ComfyUI 侧来源走 /api/upscale/comfyview 是 302 重定向，不能直接当附件下载。
function UpscaleTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const [env, setEnv] = useState<UpscaleEnv | null>(null);
  const [sources, setSources] = useState<UpscaleSource[]>([]);
  const [artifacts, setArtifacts] = useState<UpscaleArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const [scale, setScale] = useState<2 | 3 | 4>(2);
  const [task, setTask] = useState<UpscaleTask | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [playing, setPlaying] = useState<string | null>(null);
  /**
   * 是否把 ComfyUI 侧素材也列进候选。
   *
   * 后端 /api/upscale/sources 会把 **全局** COMFYUI_OUTPUT_DIR 下所有 mp4 都返回
   * （不分项目，实测该项目能列出 300 条），混进下拉框会把本项目的成片淹没，
   * 原生 select 里 300+ 选项也几乎没法选。故默认只显示项目自己的产物。
   */
  const [showComfy, setShowComfy] = useState(false);
  /** 自动生产是否带超分（项目级计划，存于 autopilot plan） */
  const [planOn, setPlanOn] = useState<boolean | null>(null);
  const [planScale, setPlanScale] = useState<2 | 3 | 4>(2);
  const [savingPlan, setSavingPlan] = useState(false);
  /** 轮询定时器句柄（卸载/切任务时必须清掉，否则会一直打后端） */
  const pollRef = React.useRef<number | null>(null);

  const stopPoll = React.useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const loadArtifacts = React.useCallback(async () => {
    try {
      const d = await upscaleApi.list(projectKey);
      setArtifacts(d.items || []);
    } catch {
      setArtifacts([]);
    }
  }, [projectKey]);

  const load = React.useCallback(async () => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    try {
      // env 失败不视为致命：把原因展示出来比整页报错更有用
      const [envRes, srcRes, planRes] = await Promise.allSettled([
        upscaleApi.env(),
        upscaleApi.sources(projectKey),
        autopilotApi.plan(projectKey),
      ]);
      if (envRes.status === 'fulfilled') setEnv(envRes.value);
      else setEnv(null);
      if (planRes.status === 'fulfilled') {
        const pl = (planRes.value?.plan || {}) as Record<string, unknown>;
        setPlanOn(pl.enable_upscale !== false);
        const s = Number(pl.upscale_scale);
        setPlanScale(s === 3 || s === 4 ? (s as 3 | 4) : 2);
      } else {
        setPlanOn(null);
      }
      if (srcRes.status === 'fulfilled') {
        const items = srcRes.value.items || [];
        setSources(items);
        // 默认优先选「成片」（自动生产刚出的成品），省掉一次手动选择
        setSelected((prev) => prev || (items.find((i) => i.kind === '成片') || items[0])?.path || '');
      } else {
        setError(srcRes.reason instanceof Error ? srcRes.reason.message : t('upscale.loadFailed'));
        setSources([]);
      }
      await loadArtifacts();
    } finally {
      setLoading(false);
    }
  }, [projectKey, loadArtifacts, t]);

  useEffect(() => { void load(); }, [load]);

  // 组件卸载时停掉轮询
  useEffect(() => () => stopPoll(), [stopPoll]);

  /** 开始轮询某个任务；done/error 时自动停机并刷新产物列表 */
  const startPoll = React.useCallback((taskId: string) => {
    stopPoll();
    pollRef.current = window.setInterval(async () => {
      try {
        const st = await upscaleApi.status(taskId);
        setTask(st);
        if (st.status === 'done' || st.status === 'error') {
          stopPoll();
          if (st.status === 'done') await loadArtifacts();
        }
      } catch (e) {
        stopPoll();
        setError(e instanceof Error ? e.message : t('upscale.statusFailed'));
      }
    }, 2500);
  }, [stopPoll, loadArtifacts, t]);

  const submit = async () => {
    if (!selected) return;
    setSubmitting(true);
    setError('');
    setPlaying(null);
    try {
      const res = await upscaleApi.submit({
        project_name: projectKey,
        video_path: selected,
        scale,
        // 成片含配音，必须保留音轨
        attach_audio: true,
      });
      setTask({ task_id: res.task_id, status: 'pending', progress: 0, message: '' });
      startPoll(res.task_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('upscale.submitFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  /** 保存「自动生产时是否带超分」到项目计划（后端只认 PLAN_DEFAULTS 里的字段） */
  const savePlan = async (patch: { enable_upscale?: boolean; upscale_scale?: 2 | 3 | 4 }) => {
    setSavingPlan(true);
    setError('');
    try {
      const res = await autopilotApi.setPlan(projectKey, patch);
      const pl = (res?.plan || {}) as Record<string, unknown>;
      setPlanOn(pl.enable_upscale !== false);
      const s = Number(pl.upscale_scale);
      setPlanScale(s === 3 || s === 4 ? (s as 3 | 4) : 2);
    } catch (e) {
      setError(e instanceof Error ? e.message : t('upscale.planSaveFailed'));
    } finally {
      setSavingPlan(false);
    }
  };

  if (loading) return (
    // 骨架对齐真实区块：标题行 → 链路自检卡 → 计划卡 → 源选择卡
    <div className="space-y-4" role="status" aria-live="polite" aria-label={t('common.loading')}>
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 space-y-2">
          <Skeleton className="h-5 w-28" />
          <Skeleton className="h-4 w-56" />
        </div>
        <Skeleton className="h-8 w-20" />
      </div>
      <Skeleton className="h-32 rounded-lg" />
      <Skeleton className="h-32 rounded-lg" />
      <Skeleton className="h-44 rounded-lg" />
    </div>
  );

  const ready = !!env?.available;
  const busy = task?.status === 'pending' || task?.status === 'running';
  /** 本项目自身产物（成片 / 片段 / 已有超分）；ComfyUI 侧是全局素材池，默认折叠 */
  const visibleSources = showComfy
    ? sources
    : sources.filter((s) => !s.kind.startsWith('ComfyUI'));
  const source = sources.find((s) => s.path === selected);
  const srcUrl = source?.url || '';
  // 仅 /api/upscale/<project>/<name> 支持 ?download=1（comfyview 是 302，不能当附件）
  const downloadUrl = (u?: string) =>
    u && u.startsWith('/api/upscale/') && !u.includes('comfyview') ? `${u}?download=1` : '';

  const statusText = () => {
    if (!task) return '';
    if (task.status === 'pending') return t('upscale.queued');
    if (task.status === 'running') return t('upscale.running');
    if (task.status === 'done') return t('upscale.done');
    return t('upscale.failed');
  };

  const res = task?.result;
  const fmtResolution = (v?: { width?: number; height?: number }) =>
    v?.width && v?.height ? `${v.width}×${v.height}` : '—';

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-lg font-semibold text-ink-1">{t('upscale.title')}</h3>
          <p className="text-sm text-ink-2 mt-0.5">{t('upscale.subtitle')}</p>
        </div>
        <Button size="sm" variant="secondary" onClick={load} disabled={loading || busy}>
          {t('common.refresh')}
        </Button>
      </div>

      {/* 链路自检 */}
      <div
        className={`p-3 rounded-lg border text-sm ${
          ready
            ? 'bg-success-subtle border-success/30 text-success-strong'
            : 'bg-warning-subtle border-warning/30 text-warning-strong'
        }`}
      >
        <div className="flex items-center justify-between gap-3">
          <span className="font-medium">
            {ready ? t('upscale.envOk') : t('upscale.envBad')}
          </span>
          <span className="text-xs">
            {t('upscale.engine')}:{' '}
            {env?.default_engine === 'legacy-flashvsr'
              ? t('upscale.engineLegacy')
              : t('upscale.engineTe')}
          </span>
        </div>
        {env && (
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
            <span className="inline-flex items-center gap-1">{t('upscale.comfyOnline')}: {env.comfy_online ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.modelReady')}: {env.model_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.teReady')}: {env.te_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
            <span className="inline-flex items-center gap-1">{t('upscale.legacyReady')}: {env.legacy_ready ? <Check className="h-3.5 w-3.5 text-success" /> : <X className="h-3.5 w-3.5 text-danger" />}</span>
          </div>
        )}
        {!ready && (env?.reasons?.length ?? 0) > 0 && (
          <div className="mt-2">
            <p className="text-xs opacity-90">{t('upscale.envHint')}</p>
            <ul className="mt-1 list-disc list-inside text-xs opacity-90 space-y-0.5">
              {(env?.reasons || []).map((r) => <li key={r}>{r}</li>)}
            </ul>
          </div>
        )}
        {/* 说明流水线默认开启超分且失败即跳过，避免用户以为「没超分 = 坏了」 */}
        <p className="mt-2 text-xs opacity-80">{t('upscale.pipelineTip')}</p>
      </div>

      {/* 自动生产是否带超分 —— 超分默认开启，且单集耗时会明显变长，
          必须给一个真正的关闭入口（之前 enable_upscale 不在 PLAN_DEFAULTS 里，
          接口会把该字段过滤掉，等于关不掉）。 */}
      {planOn !== null && (
        <div className="bg-surface rounded-lg border border-line p-4">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="text-sm font-medium text-ink-1">
                {t('upscale.planToggle')}
              </p>
              <p className="text-xs text-ink-2 mt-1">
                {t('upscale.planToggleHint')}
              </p>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <span className={`text-xs font-medium ${planOn ? 'text-success-strong' : 'text-ink-2'}`}>
                {planOn ? t('upscale.on') : t('upscale.off')}
              </span>
              <button
                role="switch"
                aria-checked={planOn}
                disabled={savingPlan}
                onClick={() => savePlan({ enable_upscale: !planOn })}
                className={`relative w-11 h-6 rounded-full transition-colors disabled:opacity-50 ${FOCUS_RING} ${
                  planOn ? 'bg-brand' : 'bg-line-strong'
                }`}
              >
                <span
                  className={`absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-surface shadow transition-transform ${
                    planOn ? 'translate-x-5' : ''
                  }`}
                />
              </button>
            </div>
          </div>

          {/* 自动生产的超分倍率（与手工超分独立配置） */}
          <div className="mt-3 pt-3 border-t border-line flex items-center gap-3">
            <span className="text-xs text-ink-2">{t('upscale.planScale')}</span>
            <div className="flex gap-2">
              {([2, 3, 4] as const).map((n) => (
                <button
                  key={n}
                  disabled={savingPlan || !planOn}
                  onClick={() => savePlan({ upscale_scale: n })}
                  className={`px-3 py-1 rounded-md text-xs font-medium transition-all disabled:opacity-50 ${FOCUS_RING} ${
                    planScale === n
                      ? 'bg-brand text-white'
                      : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1'
                  }`}
                >
                  {t('upscale.scaleTimes', { n })}
                </button>
              ))}
            </div>
            <span className="text-xs text-ink-3">{t('upscale.planScaleHint')}</span>
          </div>
        </div>
      )}

      {error && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-sm text-danger-strong">
          {error}
        </div>
      )}

      {/* 选择源 + 倍率 + 发起 */}
      <div className="bg-surface rounded-lg border border-line p-4 space-y-4">
        {sources.length === 0 ? (
          <EmptyState
            icon={<ZoomIn className="h-10 w-10" />}
            title={t('upscale.sourceEmpty')}
            description={t('upscale.sourceEmptyTip')}
          />
        ) : (
          <>
            <div className="grid md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-ink-2 mb-1.5">
                  {t('upscale.selectSource')}
                </label>
                <select
                  value={selected}
                  onChange={(e) => { setSelected(e.target.value); setPlaying(null); }}
                  disabled={busy}
                  className={`w-full px-3 py-2 text-sm border border-line rounded-lg bg-surface text-ink-1 ${FOCUS_RING}`}
                >
                  {Array.from(new Set(visibleSources.map((s) => s.kind))).map((kind) => (
                    <optgroup key={kind} label={kind}>
                      {visibleSources.filter((s) => s.kind === kind).map((s) => (
                        <option key={s.path} value={s.path}>
                          {s.name} · {s.size_mb} MB
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
                {/* ComfyUI 侧是全局素材池（不分项目），默认折叠避免淹没本项目成片 */}
                <label className="mt-2 flex items-center gap-2 text-xs text-ink-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={showComfy}
                    onChange={(e) => setShowComfy(e.target.checked)}
                    className={`rounded border-line accent-brand ${FOCUS_RING}`}
                  />
                  {t('upscale.showComfy', {
                    n: sources.length - sources.filter((s) => !s.kind.startsWith('ComfyUI')).length,
                  })}
                </label>
                {source && (
                  <p className="text-xs text-ink-2 mt-1.5">
                    {source.mtime} · {source.size_mb} MB
                  </p>
                )}
              </div>

              <div>
                <label className="block text-xs font-medium text-ink-2 mb-1.5">
                  {t('upscale.scale')}
                </label>
                <div className="flex gap-2">
                  {([2, 3, 4] as const).map((n) => (
                    <button
                      key={n}
                      onClick={() => setScale(n)}
                      disabled={busy}
                      className={`px-4 py-2 rounded-lg text-sm font-medium transition-all ${FOCUS_RING} ${
                        scale === n
                          ? 'bg-brand text-white shadow-lg'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1'
                      }`}
                    >
                      {t('upscale.scaleTimes', { n })}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-ink-2 mt-1.5">
                  {t('upscale.scaleHint')}
                </p>
              </div>
            </div>

            {/* 源预览：确认选中的是哪一个视频 */}
            {srcUrl && playing === 'source' && (
              <video src={srcUrl} controls className="w-full rounded-lg bg-black" />
            )}

            <div className="flex gap-2 flex-wrap">
              <Button onClick={submit} disabled={!ready || !selected || submitting || busy}>
                {busy ? t('upscale.running') : t('upscale.start')}
              </Button>
              {srcUrl && (
                <Button
                  variant="secondary"
                  onClick={() => setPlaying(playing === 'source' ? null : 'source')}
                >
                  {playing === 'source' ? t('common.close') : t('upscale.preview')}
                </Button>
              )}
              {!ready && (
                <span className="text-xs text-warning-strong self-center">
                  {t('upscale.notReadyTip')}
                </span>
              )}
            </div>
          </>
        )}
      </div>

      {/* 任务进度 / 结果 */}
      {task && (
        <div className="bg-surface rounded-lg border border-line p-4 space-y-3">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold text-ink-1">{statusText()}</span>
            <span className="text-xs text-ink-2">
              {t('upscale.progress')} {task.progress || 0}%
            </span>
          </div>

          <div className="h-2 rounded-full bg-surface-2 overflow-hidden">
            <div
              className={`h-full transition-all ${
                task.status === 'error' ? 'bg-danger' : 'bg-brand'
              }`}
              style={{ width: `${Math.min(100, task.progress || 0)}%` }}
            />
          </div>

          {task.message && (
            <p className="text-xs text-ink-2">{task.message}</p>
          )}
          {task.status === 'error' && task.error && (
            <p className="text-xs text-danger-strong break-all">{task.error}</p>
          )}

          {task.status === 'done' && res && (
            <div className="pt-3 border-t border-line space-y-3">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                <div>
                  <div className="text-ink-2">{t('upscale.before')}</div>
                  <div className="font-medium text-ink-1">{fmtResolution(res.before)}</div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.after')}</div>
                  <div className="font-medium text-ink-1">{fmtResolution(res.after)}</div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.elapsed')}</div>
                  <div className="font-medium text-ink-1">
                    {res.elapsed_sec != null ? `${res.elapsed_sec}s` : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-ink-2">{t('upscale.resolution')}</div>
                  <div className="font-medium text-ink-1">
                    {res.after?.size_mb != null ? `${res.after.size_mb} MB` : '—'}
                  </div>
                </div>
              </div>

              {/* 音轨保留情况：无声超分是这里最容易踩的坑 */}
              <p className={`text-xs ${res.after?.has_audio ? 'text-success-strong' : 'text-warning-strong'}`}>
                {res.after?.has_audio ? t('upscale.audioKept') : t('upscale.audioLost')}
              </p>

              {res.output_url && (
                <>
                  <video src={res.output_url} controls className="w-full rounded-lg bg-black" />
                  <div className="flex gap-2">
                    <a
                      href={downloadUrl(res.output_url) || res.output_url}
                      className={`inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-line text-ink-1 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}
                    >
                      {t('common.download')}
                    </a>
                    <span className="text-xs text-ink-2 self-center">
                      {t('upscale.confirmClose')}
                    </span>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {/* 已生成的超分产物 */}
      {artifacts.length > 0 && (
        <div className="bg-surface rounded-lg border border-line p-4">
          <h4 className="text-sm font-semibold text-ink-1 mb-3">
            {t('upscale.artifacts')}（{artifacts.length}）
          </h4>
          <div className="space-y-2">
            {artifacts.map((a) => (
              <div
                key={a.name}
                className="flex items-center justify-between gap-3 py-2 border-b border-line last:border-0"
              >
                <div className="min-w-0">
                  <p className="text-sm text-ink-1 truncate">{a.name}</p>
                  <p className="text-xs text-ink-2">{a.mtime} · {a.size_mb} MB</p>
                </div>
                <div className="flex gap-2 shrink-0">
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setPlaying(playing === a.name ? null : a.name)}
                  >
                    {playing === a.name ? t('common.close') : t('upscale.preview')}
                  </Button>
                  {downloadUrl(a.url) && (
                    <a
                      href={downloadUrl(a.url)}
                      className={`inline-flex items-center px-3 py-1.5 text-sm rounded-lg border border-line text-ink-1 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}
                    >
                      {t('common.download')}
                    </a>
                  )}
                </div>
              </div>
            ))}
          </div>
          {playing && artifacts.some((a) => a.name === playing) && (
            <video
              src={artifacts.find((a) => a.name === playing)?.url}
              controls
              className="w-full mt-3 rounded-lg bg-black"
            />
          )}
        </div>
      )}
    </div>
  );
}

// ========== AI总控（项目内右侧常驻面板） ==========
// 原先它是工作台里的第 10 个标签页，排在最后、还会换行，用户反馈「进去后找不到了」。
// 现改为右侧常驻、可折叠：与标签内容并排，切换标签页时对话不丢失。
// 工具名 → 人话。用户在总控面板看到的应该是「生产一集」而不是 `produce_episode`。
// `t()` 找不到键时会**原样返回 key**，所以缺映射时回退到工具名本身（不显示 `toolLabel.xxx`）。
function toolLabel(t: (k: string, p?: Record<string, string | number>) => string, name: string): string {
  const key = `toolLabel.${name}`;
  const hit = t(key);
  return hit === key ? name : hit;
}

// 生产状态轮询：让用户在总控面板里随时看到「现在在生成什么」。
//
// 背景：此前总控面板只显示「总控执行中 · 已完成 N 步」+ 工具名，用户完全不知道
// 后台正在拍哪一集、走到哪个环节。后端 `current` 里其实有完整的
// 集号 / 章节标题 / 阶段 / 百分比，这里把它拉到前端常驻展示。
//
// ⚠️ 只在**生产进行中**才轮询（idle 时 12s 一次慢轮询兜底恢复），避免空转打接口；
//    组件卸载即停，不留后台定时器。
function useProductionStatus(projectKey: string) {
  const [current, setCurrent] = useState<any>(null);
  useEffect(() => {
    if (!projectKey) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      let next = 3000;
      try {
        const st = await autopilotApi.status(projectKey);
        if (!alive) return;
        const cur = st?.current || null;
        setCurrent(cur);
        // 有活儿 → 3s 紧轮询；没活儿 → 12s 慢轮询（等新任务起来）
        next = cur ? 3000 : 12000;
      } catch {
        if (!alive) return;
        next = 12000;
      }
      if (alive) timer = setTimeout(tick, next);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [projectKey]);
  return current;
}

// 正在生产的状态条 → 实时状态卡（合并了原先叠加的两条进度条）：
//   一行「正在生产」= 哪一集 / 哪一步 / 几成（/api/autopilot/status）；
//   一行「ComfyUI 采样」= 解析 comfyui 日志 tqdm 的 N/M 实时步数（如 10/19）。
// 有生产任务才轮询日志源，纯聊天时整卡不渲染。
function AgentStatusCard({ projectKey }: { projectKey: string }) {
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

/** 总控执行轨迹：像 Agent 工作台一样把「它正在干什么」摊开 —— 一次工具调用一个节点
 *  （工具名徽标 + 耗时 + 结果摘要），执行中最新一步高亮、末尾挂「等待下一步」。
 *  live=true（执行中）始终展开、标题实时计时；job 结束后作为一条 run 消息留在
 *  对话里（默认展开、可收起）——此前 run 结束即被置 null，「它做过什么」无处可查。 */
function AgentTrace({ steps, status, startedAt, live = false }: {
  steps: AgentStep[];
  status: string;
  startedAt?: number;
  live?: boolean;
}) {
  const [open, setOpen] = useState(true);
  // 执行中每秒重渲染一次，让标题里的耗时走秒
  const [, tick] = useState(0);
  useEffect(() => {
    if (!live) return;
    const id = window.setInterval(() => tick(v => v + 1), 1000);
    return () => window.clearInterval(id);
  }, [live]);

  const running = status === 'running';
  const failed = steps.some((s) => !s.ok && !s.blocked);
  const elapsed = live && startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : null;
  const titleKey =
    running ? 'live.agentRunning'
    : status === 'failed' ? 'live.agentRunFailed'
    : status === 'killed' ? 'live.agentRunKilled'
    : status === 'timeout' ? 'live.agentTimeout'
    : 'live.agentRunDone';

  return (
    <div className={`mr-6 rounded-lg border bg-surface ${running ? 'glow-brand border-brand/30' : 'border-line'}`}>
      <div className="flex items-center gap-2 px-3 py-2 text-xs">
        {running ? (
          <span className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-brand/40 border-t-brand" aria-hidden="true" />
        ) : (
          <span className={`h-2 w-2 shrink-0 rounded-full ${failed ? 'bg-danger' : 'bg-success'}`} aria-hidden="true" />
        )}
        <span className={`shrink-0 font-medium tabular-nums ${running ? 'text-brand' : 'text-ink-2'}`}>
          {t(titleKey, { n: steps.length, s: elapsed ?? 0 })}
        </span>
        {!running && (
          <button
            type="button"
            onClick={() => setOpen(v => !v)}
            aria-expanded={open}
            title={open ? t('live.collapse') : t('live.expand')}
            className={`ml-auto rounded p-0.5 text-ink-3 transition-colors hover:text-ink-1 ${FOCUS_RING}`}
          >
            <svg
              className={`h-3.5 w-3.5 transition-transform ${open ? 'rotate-180' : ''}`}
              fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
        )}
      </div>
      {open && (
        <ol className="relative mx-3 mb-3 space-y-2 border-l border-line pl-3.5">
          {steps.map((s, i) => {
            const isCurrent = live && i === steps.length - 1;
            return (
              <li key={i} className="relative text-xs leading-snug">
                <span
                  className={`absolute -left-[22px] top-0 flex h-3.5 w-3.5 items-center justify-center rounded-full border bg-surface ${
                    s.blocked
                      ? 'border-warning/50 text-warning-strong'
                      : s.ok
                        ? 'border-success/50 text-success-strong'
                        : 'border-danger/50 text-danger-strong'
                  }`}
                  aria-hidden="true"
                >
                  {s.blocked
                    ? <AlertTriangle className="h-2.5 w-2.5" />
                    : s.ok
                      ? <Check className="h-2.5 w-2.5" />
                      : <X className="h-2.5 w-2.5" />}
                </span>
                <div className={isCurrent ? 'rounded-md bg-brand-subtle/40 px-1.5 py-1' : ''}>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="rounded bg-brand-subtle px-1.5 py-0.5 font-medium text-brand" title={s.tool}>
                      {toolLabel(t, s.tool)}
                    </span>
                    {s.elapsed_sec != null && (
                      <span className="tabular-nums text-ink-3">{t('live.elapsed', { s: Math.round(s.elapsed_sec) })}</span>
                    )}
                  </div>
                  <p className="mt-0.5 break-all text-ink-2">
                    {s.summary}
                    {s.cached ? t('chat.cached') : ''}
                  </p>
                </div>
              </li>
            );
          })}
          {running && (
            <li className="relative text-xs text-ink-3">
              <span
                className="absolute -left-[22px] top-0 flex h-3.5 w-3.5 items-center justify-center rounded-full border border-brand/40 bg-surface"
                aria-hidden="true"
              >
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
              </span>
              {t('live.waitingNext')}
            </li>
          )}
        </ol>
      )}
    </div>
  );
}

function ChatPanel({ projectKey, onClose }: { projectKey: string; onClose: () => void }) {
  // 会话缓存（@/agentSession）：切菜单/收起面板时组件卸载，消息与进行中的 job
  // 存在模块级 session 里，重挂载时原样恢复并继续跟踪同一个 job。
  const [messages, setMessages] = useState<ChatMsg[]>(() => [...getAgentSession(projectKey).messages]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  // 自主执行模式：默认开启。指令交给总控模型自己决策并调用工具，全过程无需人工确认。
  const [autoMode, setAutoMode] = useState(true);
  const [run, setRun] = useState<{ steps: AgentStep[]; status: string; startedAt?: number } | null>(null);
  const [toolCount, setToolCount] = useState(0);
  const [killOn, setKillOn] = useState(false);
  const messagesEndRef = React.useRef<HTMLDivElement>(null);
  // 审计 P2-35（2026-09-29）：发送路径的 trackJob 此前没传取消守卫 —— 面板卸载后
  // 轮询最长还会空转 35 分钟并对已卸载组件 setState。与「恢复跟踪」路径同一口径：
  // 卸载即让位（session 里的 job 信息保留，重挂载的新实例接手）。
  const panelAliveRef = React.useRef(true);
  React.useEffect(() => {
    panelAliveRef.current = true;
    return () => { panelAliveRef.current = false; };
  }, []);

  // 拖拽调宽（2026-09-29 用户需求）：面板左缘手柄，按住左右拖改宽度，
  // 钳制 [300, 600]；松手落 localStorage 记住偏好；双击恢复默认 340。
  // 宽度经 CSS 变量 --chat-w 注入 lg:w-[var(--chat-w)] —— 小屏（<lg）本就
  // w-full 全宽堆叠，变量与手柄都不生效，行为零变化。
  const CHAT_W_DEFAULT = 340;
  const CHAT_W_MIN = 300;
  const CHAT_W_MAX = 600;
  const [chatW, setChatW] = useState<number>(() => {
    try {
      const v = Number(window.localStorage.getItem('mjscxt.chatPanelWidth'));
      if (Number.isFinite(v) && v >= CHAT_W_MIN && v <= CHAT_W_MAX) return Math.round(v);
    } catch { /* localStorage 不可用：用默认宽度 */ }
    return CHAT_W_DEFAULT;
  });
  const chatWRef = React.useRef(chatW);
  chatWRef.current = chatW;

  const onHandleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = chatWRef.current;
    const prevCursor = document.body.style.cursor;
    const prevSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    const onMove = (ev: MouseEvent) => {
      // 面板在右侧：往左拖（clientX 变小）= 变宽
      const next = Math.round(startW + (startX - ev.clientX));
      setChatW(Math.max(CHAT_W_MIN, Math.min(CHAT_W_MAX, next)));
    };
    const onUp = () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = prevCursor;
      document.body.style.userSelect = prevSelect;
      try { window.localStorage.setItem('mjscxt.chatPanelWidth', String(chatWRef.current)); } catch { /* ignore */ }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };

  const onHandleDoubleClick = () => {
    setChatW(CHAT_W_DEFAULT);
    try { window.localStorage.setItem('mjscxt.chatPanelWidth', String(CHAT_W_DEFAULT)); } catch { /* ignore */ }
  };

  // 追加消息：session 是唯一真源，state 只是它的投影——组件卸载后 session 仍会更新，
  // 回来时轨迹不丢（直接写 setMessages 的话，卸载期间发生的事就没人记了）。
  const pushMsg = (m: ChatMsg) => {
    const s = getAgentSession(projectKey);
    s.messages = [...s.messages, m];
    setMessages(s.messages);
  };

  // 加载该项目的历史对话。会话缓存非空时直接还原（保留 run 轨迹与进行中的 job），
  // 不回后端重拉——重拉会把结构化轨迹洗掉；「刷新」按钮传 force 才真正重拉。
  const loadHistory = async (force = false) => {
    const s = getAgentSession(projectKey);
    if (!force && s.messages.length > 0) {
      setMessages([...s.messages]);
      return;
    }
    try {
      const d = await chatApi.history(projectKey);
      s.messages = (d.messages || []) as ChatMsg[];
      setMessages(s.messages);
    } catch (err) {
      console.error('加载对话历史失败:', err);
    }
  };

  useEffect(() => { void loadHistory(); }, [projectKey]);

  // 拉取总控可用工具数与急停状态（失败不影响对话，静默降级）
  useEffect(() => {
    agentApi.tools()
      .then((d) => { setToolCount(d.count || 0); setKillOn(!!d.kill?.on); })
      .catch(() => {});
  }, []);

  // 恢复跟踪：切菜单/收起面板前若有进行中的 job，回来后继续轮询同一个 job。
  // （StrictMode 双挂载/组件卸载时通过 cancelled 停掉旧循环，session 状态留给新实例）
  useEffect(() => {
    const s = getAgentSession(projectKey);
    if (!s.runningJobId) return;
    let cancelled = false;
    void trackJob(s.runningJobId, s.runningStartedAt ?? Date.now(), () => cancelled);
    return () => { cancelled = true; };
  }, [projectKey]);

  const toggleKill = async () => {
    try {
      const d = await agentApi.setKill(!killOn, !killOn ? '前端手动急停' : '');
      setKillOn(!!d.kill?.on);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('chat.killFailed'));
    }
  };

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  /** 轮询一个总控 job 到结束：实时刷新 run（AgentTrace 时间线），结束后把轨迹与回复落进对话。
   *  isCancelled=组件卸载/重挂载时停掉旧循环——不清 session 状态，重挂载的新实例会接手。 */
  const trackJob = async (jobId: string, startedAt?: number, isCancelled?: () => boolean) => {
    const s = getAgentSession(projectKey);
    s.runningJobId = jobId;
    s.runningStartedAt = startedAt ?? s.runningStartedAt ?? Date.now();
    setSending(true);
    setRun({ steps: [], status: 'running', startedAt: s.runningStartedAt });
    let failures = 0;
    const deadline = Date.now() + 35 * 60 * 1000; // 兜底，避免异常时永久轮询
    for (;;) {
      await new Promise(r => setTimeout(r, 1200));
      if (isCancelled?.()) return; // 旧实例让位：session 里的 job 信息由重挂载的新实例接手
      let job;
      try {
        job = await agentApi.job(jobId);
        failures = 0;
      } catch {
        failures += 1;
        // 后端连续不可达（重启中/挂了）：停止跟踪，避免空转到 35 分钟兜底
        if (failures >= 5) { setError(t('live.pollFailed')); break; }
        if (Date.now() > deadline) break;
        continue;
      }
      setRun({ steps: job.steps || [], status: job.status || 'running', startedAt: s.runningStartedAt ?? undefined });
      if (job.status !== 'running') {
        pushMsg({ role: 'assistant', kind: 'run', steps: job.steps || [], status: job.status || 'done', timestamp: new Date().toISOString() });
        if (job.reply) {
          pushMsg({ role: 'assistant', content: job.reply, timestamp: new Date().toISOString() });
        } else if (job.error) {
          setError(job.error);
        }
        break;
      }
      if (Date.now() > deadline) { setError(t('chat.timeout')); break; }
    }
    s.runningJobId = null;
    s.runningStartedAt = null;
    setRun(null);
    setSending(false);
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setSending(true);
    setError('');
    // 乐观渲染用户消息
    pushMsg({ role: 'user', content: text, timestamp: new Date().toISOString() });
    setInput('');
    try {
      // 纯聊天模式：走老链路，只做对话 + 抽取创作设定
      if (!autoMode) {
        const data = await chatApi.send(text, projectKey);
        if (data.success && data.reply) {
          pushMsg({ role: 'assistant', content: data.reply, timestamp: new Date().toISOString() });
        } else {
          setError(t('chat.noReply'));
        }
        return;
      }

      // 自主执行模式：下发任务 → trackJob 轮询 → 时间线摊开「它自己做了什么」
      const started = await agentApi.send(text, projectKey);
      if (!started.success || !started.job_id) {
        setError(t('chat.startFailed'));
        return;
      }
      await trackJob(started.job_id, Date.now(), () => !panelAliveRef.current);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('chat.sendFailed'));
      const s = getAgentSession(projectKey);
      s.runningJobId = null;
      s.runningStartedAt = null;
      setRun(null);
    } finally {
      setSending(false);
    }
  };

  return (
    <aside
      // h-[calc(100vh-7rem)] = 视口高 −（顶栏 ~63px + main 上下 padding 48px），
      // 让面板与左列内容等高、上下贯通；sticky 使其随页面滚动保持停靠。
      // glass：半透明 + 背景模糊，盖在科技感氛围层上（常驻 chrome 才用 blur）。
      // 2026-09-29：宽度可拖拽 —— lg 宽度由 CSS 变量 --chat-w 注入（左缘手柄拖动
      // 调节，双击恢复 340，偏好落 localStorage）；小屏 <lg 仍 w-full 全宽堆叠。
      className="glass relative flex w-full flex-col overflow-hidden rounded-xl border border-line lg:sticky lg:top-0 lg:h-[calc(100vh-7rem)] lg:w-[var(--chat-w)] lg:shrink-0 min-h-[420px]"
      style={{ '--chat-w': `${chatW}px` } as React.CSSProperties}
    >
      {/* 拖拽调宽手柄：贴左缘 6px 竖条，hover 高亮；小屏堆叠全宽无意义 → hidden，lg 才显示 */}
      <div
        onMouseDown={onHandleMouseDown}
        onDoubleClick={onHandleDoubleClick}
        title={t('chat.resizeHint')}
        className="absolute left-0 top-0 z-10 hidden h-full w-1.5 cursor-col-resize bg-transparent transition-colors hover:bg-brand/40 lg:block"
      />
      {/* 头部：与工作台其他面板一致的白底 + 灰边 + indigo 强调 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-line shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="w-7 h-7 rounded-lg bg-brand-subtle flex items-center justify-center shrink-0">
            <MessageSquare className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <h3 className="font-semibold text-ink-1 leading-tight">{t('chat.panelTitle')}</h3>
            <div className="flex items-center gap-1.5 mt-0.5">
              <p className="text-xs text-ink-2 leading-tight">
                {autoMode ? t('chat.autoExec', { n: toolCount || '…' }) : t('chat.chatOnly')}
              </p>
              <button
                onClick={() => setAutoMode(v => !v)}
                title={autoMode ? t('chat.switchToChat') : t('chat.switchToAuto')}
                className={`text-xs leading-none px-1.5 py-0.5 rounded border transition-colors ${FOCUS_RING} ${
                  autoMode
                    ? 'border-brand/30 text-brand bg-brand-subtle'
                    : 'border-line text-ink-2'
                }`}
              >
                {autoMode ? t('chat.auto') : t('chat.chat')}
              </button>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-0.5 shrink-0">
          {autoMode && (
            <button
              onClick={toggleKill}
              title={killOn ? t('chat.releaseKill') : t('chat.kill')}
              className={`p-1.5 rounded-lg transition-colors ${FOCUS_RING} ${
                killOn
                  ? 'text-danger bg-danger-subtle'
                  : 'text-ink-3 hover:text-danger hover:bg-danger-subtle'
              }`}
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            </button>
          )}
          <Button variant="ghost" size="sm" onClick={loadHistory} title={t('chat.refreshHistory')}>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </Button>
          <Button variant="ghost" size="sm" onClick={onClose} title={t('chat.collapse')}>
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 5l7 7-7 7M5 5l7 7-7 7" />
            </svg>
          </Button>
        </div>
      </div>

      {/* 实时状态卡：正在生产（哪一集/哪一步/几成）+ ComfyUI 采样进度（如 10/19）。
          此处原先是两条几乎相同的进度条叠加，已合并为一张卡。 */}
      <AgentStatusCard projectKey={projectKey} />

      {error && (
        <div className="mx-3 mt-3 p-2 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-xs shrink-0">
          {error}
        </div>
      )}

      {/* 消息区：浅色底以区别于面板头部/输入区，形成「对话」区域感。
          注意：空态与消息列表要二选一渲染 —— 若把滚动哨兵 <div> 和 h-full 的空态
          放在同一个 space-y-3 容器里，哨兵会额外吃到 12px margin 而撑出滚动条。 */}
      <div className="flex-1 min-h-0 overflow-y-auto bg-surface-2">
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center px-4">
            <EmptyState
              icon={<MessageSquare className="h-10 w-10" />}
              title={autoMode ? t('chat.autoEmptyTitle') : t('chat.chatEmptyTitle')}
              description={autoMode ? t('chat.autoEmptyDesc') : t('chat.chatEmptyDesc')}
            />
            <div className="w-full space-y-1.5">
              {(autoMode
                ? [t('chat.suggestAuto1'), t('chat.suggestAuto2'), t('chat.suggestAuto3'), t('chat.suggestAuto4')]
                : [t('chat.suggestChat1'), t('chat.suggestChat2')]
              ).map((ex) => (
                <button
                  key={ex}
                  onClick={() => setInput(ex)}
                  className={`w-full text-left text-xs px-3 py-2 rounded-lg bg-surface border border-line text-ink-2 hover:border-brand hover:text-brand transition-colors ${FOCUS_RING}`}
                >
                  {ex}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="p-3 space-y-3">
            {messages.map((msg: ChatMsg, idx: number) =>
              msg.kind === 'run' ? (
                <AgentTrace key={idx} steps={msg.steps || []} status={msg.status || 'done'} />
              ) : (
                <div
                  key={idx}
                  className={`px-3 py-2 rounded-lg text-sm ${
                    msg.role === 'user'
                      ? 'bg-brand text-white ml-6 rounded-br-sm'
                      : 'bg-surface border border-line text-ink-1 mr-6 rounded-bl-sm'
                  }`}
                >
                  <p className="whitespace-pre-wrap break-words">{msg.content}</p>
                </div>
              )
            )}
            {/* 自主执行过程（实时）：正在跑的 job 摊开在对话流里，像 Agent 工作台一样看它干活 */}
            {run && <AgentTrace steps={run.steps} status={run.status} startedAt={run.startedAt} live />}
            {sending && !run && (
              <div className="bg-surface border border-line mr-6 px-3 py-2 rounded-lg text-sm text-ink-2 flex items-center gap-2">
                <span className="w-3 h-3 border-2 border-brand/40 border-t-brand rounded-full animate-spin inline-block" />
                {t('chat.thinking')}
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>
        )}
      </div>

      {/* 输入区 */}
      <div className="p-3 border-t border-line shrink-0 bg-surface">
        <div className="flex gap-2">
          <Input
            value={input}
            onChange={setInput}
            onEnter={sendMessage}
            placeholder={t('chat.panelPlaceholder')}
            className="flex-1 min-w-0"
          />
          <Button
            variant="brand"
            onClick={sendMessage}
            disabled={sending || !input.trim()}
            className="shrink-0"
          >
            {t('chat.send')}
          </Button>
        </div>
      </div>
    </aside>
  );
}
