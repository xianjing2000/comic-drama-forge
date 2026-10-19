/** ChatPanel —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
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
import { AgentStatusCard } from '@/components/AgentStatusCard';
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

export function ChatPanel({ projectKey, onClose }: { projectKey: string; onClose: () => void }) {
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
      console.error('Failed to load chat history:', err);
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
      const d = await agentApi.setKill(!killOn, !killOn ? t('wb.killReasonManual') : '');
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
          {sanitizeErrorMessage(error)}
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
              action={(
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => setInput(autoMode ? t('chat.suggestAuto1') : t('chat.suggestChat1'))}
                >
                  {t('chat.emptyAction')}
                </Button>
              )}
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