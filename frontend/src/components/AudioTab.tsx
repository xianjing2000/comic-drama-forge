import React, { useState } from 'react';
import { useApp } from '@/context/AppContext';
import { ttsApi, charactersApi, qcApi } from '@/api/client';
import { Button, Loading, Skeleton } from '@/components/ui';
import { CheckCircle2, Lightbulb, Mic, X } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import AssetPanel from '@/components/AssetPanel';
import type { VoiceBankItem } from '@/types';

interface AudioTabProps {
  projectKey: string;
}

/**
 * 「参考音色」页（2026-10-06 重构）：
 *  - H3 成片自带角色配音（生成时注入 voice_bank 参考音色），旧「TTS 配音任务 / 音画混音」
 *    两条任务链已下线（后端 /api/tts/{plan,generate,...} 与 /api/mix/* 已删）；
 *  - 本页只剩两件事：① 管理各角色的参考音色库（上传/试听/解绑）；
 *    ② 音频质检（客观层 ffmpeg 指标 + AI 层频谱/波形送检，走 /api/qc/audio）。
 */
export function AudioTab({ projectKey }: AudioTabProps) {
  const { t } = useApp();
  const toast = useToast();
  /**
   * C4（2026-09-23 收口）：首屏 loader（环境自检 / 质检配置）此前是 fire-and-forget
   * —— 首屏一片空白且没有任何加载提示，用户无法区分「正在加载」和「就是没数据」。
   */
  const [bootLoading, setBootLoading] = React.useState(true);

  // 音频质检（两层：客观层 ffmpeg 指标 + AI 层频谱/波形送检）
  const [qcResult, setQcResult] = useState<any>(null);
  const [qcLoading, setQcLoading] = useState(false);
  const [qcError, setQcError] = useState('');
  /**
   * R5b（2026-10 收口）：检验对象固定为 **成片音轨**（final → FINAL_DIR 下该项目最新 mp4）。
   * 旧的 mix / merged / line 三段选择器已移除 —— 成片自带角色配音，就是唯一该检的整轨对象；
   * 多档位只会让用户猜「该选哪个」。后端仍保留 mix/merged/line 可解析（兼容旧调用），
   * 前端一律发 source: 'final'。
   */
  /** 是否带 AI 层（频谱/波形送多模态）。关掉=纯客观层，毫秒级不花模型调用 */
  const [qcWithAi, setQcWithAi] = useState(true);
  /** 质检配置（音频开关 + 三个客观层阈值，可在本页直接调档） */
  const [qcCfg, setQcCfg] = useState<any>(null);
  const [qcSaving, setQcSaving] = useState(false);
  /** TTS 环境自检（QwenTTS 是否就位；只影响「试听克隆效果」，不影响上传/管理） */
  const [ttsEnv, setTtsEnv] = useState<any>(null);

  React.useEffect(() => {
    let cancelled = false;
    setBootLoading(true);
    void (async () => {
      try {
        const res = await ttsApi.env(projectKey);
        if (!cancelled) setTtsEnv(res);
      } catch {
        /* 环境自检失败不影响音色管理主流程 */
      }
      try {
        const resp: any = await qcApi.config();
        if (!cancelled) setQcCfg(resp?.config ?? null);
      } catch {
        /* 质检未配置时不影响音色管理主流程 */
      }
      if (!cancelled) setBootLoading(false);
    })();
    return () => { cancelled = true; };
  }, [projectKey]);

  // 运行音频质检
  const handleRunAudioQc = async () => {
    setQcLoading(true);
    setQcError('');
    try {
      const r = await qcApi.checkAudio({
        project_name: projectKey,
        source: 'final',
        with_ai: qcWithAi,
      });
      setQcResult(r);
    } catch (e) {
      setQcError(e instanceof Error ? e.message : t('audio.qcFailed'));
      setQcResult(null);
    } finally {
      setQcLoading(false);
    }
  };

  // 保存音频质检阈值（只提交音频相关字段，其余配置保持不动）
  const handleSaveQcThresholds = async () => {
    if (!qcCfg) return;
    setQcSaving(true);
    try {
      await qcApi.updateConfig({
        audio_enabled: qcCfg.audio_enabled,
        audio_min_speech_ratio: Number(qcCfg.audio_min_speech_ratio),
        audio_min_mean_db: Number(qcCfg.audio_min_mean_db),
        audio_max_drift: Number(qcCfg.audio_max_drift),
      } as any);
      toast.success(t('audio.qcConfigSaved'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('settings.saveFailed'));
    } finally {
      setQcSaving(false);
    }
  };

  // 首屏加载态：与真实结构同形（音色库卡片 + 质检卡片），避免高度跳变
  if (bootLoading) {
    return (
      <div className="space-y-6" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <Skeleton className="h-32 rounded-lg" />
        <Skeleton className="h-48 rounded-lg" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 参考音色库（核心区块） */}
      <VoiceBankPanel projectKey={projectKey} />

      {/* TTS 环境自检：只在不可用时提示（可用是无声的正常态，不必占一行） */}
      {ttsEnv && !ttsEnv.available && (
        <div className="p-3 rounded-lg bg-danger-subtle border border-danger/30 text-danger-strong text-sm">
          <span className="font-medium inline-flex items-center gap-1.5">
            <X className="h-4 w-4" />{t('tts.envUnavailable')}
          </span>
          {ttsEnv.reasons && (
            <ul className="mt-2 text-sm list-disc list-inside">
              {ttsEnv.reasons.map((r: string, i: number) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* 音频质检 */}
      <div className="bg-surface rounded-lg border border-line p-4">
        <h3 className="text-lg font-semibold mb-2 flex items-center gap-2">
          <CheckCircle2 className="h-5 w-5" />{t('audio.qcTitle')}
        </h3>
        <p className="text-sm text-ink-2 mb-4">
          {t('audio.qcDescIntro')}<b>{t('audio.layerObjective')}</b>{t('audio.qcDescObjective')}<b>{t('audio.layerAi')}</b>{t('audio.qcDescAi')}
        </p>

        {/* 检验对象：固定「成片音轨」（R5b；原 mix/merged/line 选择器已移除） */}
        <div className="mb-4 text-sm text-ink-2">
          {t('audio.qcSource')}：<span className="text-ink-1 font-medium">{t('audio.sourceFinal')}</span>
        </div>

        {/* AI 层开关 + 配置状态 */}
        <div className="mb-4 p-3 rounded-lg bg-surface-2 space-y-2">
          <label className="flex items-center gap-2 text-sm text-ink-1">
            {/* 保留原生：共享组件未覆盖 checkbox */}
            <input
              type="checkbox"
              checked={qcWithAi}
              onChange={(e) => setQcWithAi(e.target.checked)}
              className="rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
            />
            {t('audio.enableAiLayer')}
            <span className="text-xs text-ink-3">{t('audio.aiLayerHint')}</span>
          </label>
          {qcCfg && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className={`px-2 py-0.5 rounded ${
                qcCfg.audio_qc_active
                  ? 'bg-success-subtle text-success-strong'
                  : 'bg-surface-2 text-ink-2'
              }`}>
                {t('audio.layerObjective')} {qcCfg.audio_qc_active ? t('audio.available') : t('audio.disabled')}
              </span>
              <span className={`px-2 py-0.5 rounded ${
                qcCfg.audio_ai_active
                  ? 'bg-brand-subtle text-brand-hover'
                  : 'bg-warning-subtle text-warning-strong'
              }`}>
                {t('audio.layerAi')} {qcCfg.audio_ai_active ? t('audio.available') : t('audio.aiNotConfigured')}
              </span>
            </div>
          )}
        </div>

        {/* 阈值（此前这些配置项在后端存在但前端没有任何入口） */}
        {/* 保留原生：这三个 number 输入带 step/min/max 约束与数值型默认值（?? 0.5 / -45），
            Input 组件未开放 step/min/max，换成 Input 会静默丢掉步进与取值范围 */}
        {qcCfg && (
          <div className="mb-4 grid grid-cols-1 md:grid-cols-3 gap-3">
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('audio.minSpeechRatio')}</div>
              <input
                type="number" step="0.05" min="0" max="1"
                value={qcCfg.audio_min_speech_ratio ?? 0.5}
                onChange={(e) => setQcCfg({ ...qcCfg, audio_min_speech_ratio: e.target.value })}
                className="w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
              />
              <div className="text-xs text-ink-3 mt-1">{t('audio.minSpeechRatioHint')}</div>
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('audio.minMeanDb')}</div>
              <input
                type="number" step="1" min="-100" max="0"
                value={qcCfg.audio_min_mean_db ?? -45}
                onChange={(e) => setQcCfg({ ...qcCfg, audio_min_mean_db: e.target.value })}
                className="w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
              />
              <div className="text-xs text-ink-3 mt-1">{t('audio.minMeanDbHint')}</div>
            </div>
            <div>
              <div className="text-xs text-ink-2 mb-1">{t('audio.maxDrift')}</div>
              <input
                type="number" step="0.05" min="0" max="5"
                value={qcCfg.audio_max_drift ?? 0.5}
                onChange={(e) => setQcCfg({ ...qcCfg, audio_max_drift: e.target.value })}
                className="w-full px-2 py-1 text-sm rounded border border-line bg-surface text-ink-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
              />
              <div className="text-xs text-ink-3 mt-1">{t('audio.maxDriftHint')}</div>
            </div>
            <div className="md:col-span-3">
              <Button onClick={handleSaveQcThresholds} disabled={qcSaving} className="text-sm">
                {qcSaving ? t('audio.saving') : t('audio.saveQcConfig')}
              </Button>
            </div>
          </div>
        )}

        <Button
          onClick={handleRunAudioQc}
          disabled={qcLoading}
          variant="brand"
          className="w-full py-2"
        >
          {qcLoading ? t('audio.qcRunning') : t('audio.runQc')}
        </Button>

        {qcError && (
          <div className="mt-3 p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
            {qcError}
          </div>
        )}
      </div>

      {/* 质检结论 */}
      {qcResult && (
        <div className="bg-surface rounded-lg border border-line p-4">
          <div className="flex items-center gap-2 mb-3 flex-wrap">
            <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
              qcResult.blocked
                ? 'bg-danger-subtle text-danger-strong'
                : qcResult.passed
                  ? 'bg-success-subtle text-success-strong'
                  : 'bg-warning-subtle text-warning-strong'
            }`}>
              {qcResult.blocked ? t('audio.criticalIssues') : qcResult.passed ? t('qc.passed') : t('audio.resultNotPassed')}
            </span>
            <span className="text-sm text-ink-2">
              {t('audio.scoreLabel')} {qcResult.score ?? '—'}
            </span>
            <span className="text-xs text-ink-3">
              {qcResult.objective_only ? t('audio.objectiveOnly') : qcResult.ai_used ? t('audio.objectivePlusAi') : t('audio.layerObjective')}
            </span>
            {qcResult.check_speech_ratio === false && (
              <span className="text-xs text-ink-3">{t('audio.fullTrackNote')}</span>
            )}
          </div>

          <div className="text-sm text-ink-1 mb-3">{qcResult.reason}</div>

          {qcResult.ai_skip_reason && (
            <div className="mb-3 p-2 rounded bg-warning-subtle text-warning-strong text-xs">
              {t('audio.aiSkipped', { reason: qcResult.ai_skip_reason })}
            </div>
          )}

          {/* 客观指标 */}
          {qcResult.metrics && (
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
              {[
                { k: t('audio.metricDuration'), v: qcResult.metrics.duration != null ? `${qcResult.metrics.duration}s` : '—' },
                { k: t('audio.metricSpeechRatio'), v: qcResult.metrics.speech_ratio != null ? `${(qcResult.metrics.speech_ratio * 100).toFixed(1)}%` : '—' },
                { k: t('audio.metricMeanDb'), v: qcResult.metrics.mean_db != null ? `${qcResult.metrics.mean_db} dB` : '—' },
                { k: t('audio.metricMaxDb'), v: qcResult.metrics.max_db != null ? `${qcResult.metrics.max_db} dB` : '—' },
              ].map((m) => (
                <div key={m.k} className="bg-surface-2 rounded p-2 text-center">
                  <div className="text-sm font-semibold text-ink-1">{m.v}</div>
                  <div className="text-xs text-ink-2">{m.k}</div>
                </div>
              ))}
            </div>
          )}

          {Array.isArray(qcResult.critical_issues) && qcResult.critical_issues.length > 0 && (
            <div className="mb-3">
              <div className="text-xs font-medium text-danger-strong mb-1">{t('audio.criticalIssues')}</div>
              <ul className="text-sm text-danger-strong space-y-0.5 list-disc list-inside">
                {qcResult.critical_issues.map((x: string, i: number) => <li key={i}>{x}</li>)}
              </ul>
            </div>
          )}
          {Array.isArray(qcResult.issues) && qcResult.issues.length > 0 && (
            <div className="mb-3">
              <div className="text-xs font-medium text-warning-strong mb-1">{t('audio.improvable')}</div>
              <ul className="text-sm text-warning-strong space-y-0.5 list-disc list-inside">
                {qcResult.issues.map((x: string, i: number) => <li key={i}>{x}</li>)}
              </ul>
            </div>
          )}

          {/* 频谱图 + 波形图（AI 层送检用的同一批图，顺序固定：先频谱后波形） */}
          {Array.isArray(qcResult.visuals) && qcResult.visuals.length > 0 && (
            <div className="space-y-2">
              <div className="text-xs text-ink-2">
                {t('audio.visualsNote')}
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                {qcResult.visuals.map((src: string, i: number) => (
                  <div key={i} className="bg-black/5 rounded p-1">
                    <img src={src} alt={i === 0 ? t('audio.spectrogram') : t('audio.waveform')} className="w-full rounded" />
                  </div>
                ))}
              </div>
            </div>
          )}

          {qcResult.file_url && (
            <audio controls src={qcResult.file_url} className="w-full mt-3" />
          )}
          <div className="mt-2 text-xs text-ink-3 break-all">{t('audio.target', { path: qcResult.path })}</div>
        </div>
      )}

      {/* ⭐ 2026-10-09 原独立 AssetPanel 已**并入本页**（用户拍板）：
          ① 配音模型逐条就绪明细 —— 本页此前只把 ttsApi.env 当门禁用，明细无处可看
          ② 关键帧清单 —— 与音色页同属「素材就绪度」核查 */}
      <AssetPanel project={projectKey} />

      {/* 页脚说明：一句话讲清本页定位（旧「三步流程」已随配音/混音任务链下线） */}
      <div className="bg-info-subtle border border-info/30 rounded-lg p-4">
        <p className="text-sm text-info-strong flex items-start gap-1.5">
          <Lightbulb className="h-4 w-4 shrink-0 mt-0.5" />
          {t('audio.voicePageNote')}
        </p>
      </div>
    </div>
  );
}

// ========== 角色声线（参考音频克隆） ==========

/** 单行试听的状态机：loading（合成中）→ url + 自动播放 → playing；失败/被拦都有可读提示 */
interface PreviewState {
  /** 正在向后端合成试听样音 */
  loading?: boolean;
  /** 合成好的试听音频 URL（拿到即自动播放） */
  url?: string;
  /** 该行播放器此刻是否在播 */
  playing?: boolean;
  /** 合成/播放**失败**原因（红字，绝不静默） */
  error?: string;
  /** 自动播放被浏览器拦截时的处置指引（黄字，不静默） */
  notice?: string;
}

/**
 * 1ms 静音 WAV 的 data URI（8kHz / 8bit / 单声道），供 primeAutoplay 在用户手势内 play()。
 * 运行时按 RIFF 规范拼字节，比手写 base64 更不容易出错；非浏览器环境（构建/SSR）直接返回空串。
 */
function silentWavDataUri(): string {
  try {
    const dataSize = 8;
    const buf = new Uint8Array(44 + dataSize);
    const dv = new DataView(buf.buffer);
    const write = (off: number, s: string) => {
      for (let i = 0; i < s.length; i++) buf[off + i] = s.charCodeAt(i);
    };
    write(0, 'RIFF');
    dv.setUint32(4, 36 + dataSize, true);
    write(8, 'WAVE');
    write(12, 'fmt ');
    dv.setUint32(16, 16, true); // fmt 块长度
    dv.setUint16(20, 1, true); // PCM
    dv.setUint16(22, 1, true); // 单声道
    dv.setUint32(24, 8000, true); // 采样率
    dv.setUint32(28, 8000, true); // 字节率
    dv.setUint16(32, 1, true); // block align
    dv.setUint16(34, 8, true); // 8bit
    write(36, 'data');
    dv.setUint32(40, dataSize, true);
    buf.fill(128, 44); // 8bit 静音 = 128
    let bin = '';
    for (let i = 0; i < buf.length; i++) bin += String.fromCharCode(buf[i]);
    if (typeof btoa !== 'function') return '';
    return `data:audio/wav;base64,${btoa(bin)}`;
  } catch {
    return '';
  }
}
const SILENT_WAV_DATA_URI = silentWavDataUri();

// 一个角色一段参考音频：上传 → 试听克隆效果 → 已绑定则显示，可解绑/重传。
// 生成视频时后端见到 voice_bank 有该角色的音频就把它作为参考音色注入 H3
// （公共提示词 <Audio N> = 角色名），所以这里的操作**即时生效于下次生成**。
function VoiceBankPanel({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  const toast = useToast();
  const [items, setItems] = React.useState<VoiceBankItem[]>([]);
  const [cloneOk, setCloneOk] = React.useState(true);
  const [loading, setLoading] = React.useState(true);
  const [busyChar, setBusyChar] = React.useState('');
  /**
   * 试听状态**按角色存**（此前是一个全局 previewUrl，播放器吊在整个列表最底部）：
   * 播放器就地展开在对应那一行，用户不用滚动去找 —— 「点了没反应」的一半原因就在这。
   */
  const [previews, setPreviews] = React.useState<Record<string, PreviewState>>({});
  /** 每行的 <audio> 元素：自动播放 / 重播都要在「用户手势内」或「合成完成瞬间」play() */
  const audioRefs = React.useRef<Record<string, HTMLAudioElement | null>>({});
  /** name → 已自动播放过的 url：effect 重跑时不重复播放 */
  const autoPlayedRef = React.useRef<Record<string, string>>({});
  /** 静音解锁媒体（见 primeAutoplay）：只为在用户手势内拿到自动播放许可 */
  const primerRef = React.useRef<HTMLAudioElement | null>(null);
  /** toast 引用：避免把每次渲染可能新建的 toast 放进 effect 依赖里造成循环 */
  const toastRef = React.useRef(toast);
  toastRef.current = toast;
  /** 资产角色名（优先候选）：资产库角色 = 剧本角色，供「未绑定也能先传」 */
  const [assetChars, setAssetChars] = React.useState<string[]>([]);

  const patchPreview = React.useCallback((name: string, patch: Partial<PreviewState>) => {
    setPreviews((prev) => ({ ...prev, [name]: { ...prev[name], ...patch } }));
  }, []);

  /**
   * 新增文案的兜底：本次改动范围（AudioTab.tsx / client.ts）**不含** locales/**，
   * 语言包若还没补这几个 key，t() 会把 key 原样返回（i18n/index.ts:44），界面就会冒出
   * 「audio.voicePreviewCooking」这种裸 key —— 这里回落中文，绝不显示裸 key。
   */
  const tf = React.useCallback(
    (key: string, fallback: string, params?: Record<string, string | number>) => {
      const v = t(key, params);
      return v === key ? fallback : v;
    },
    [t]
  );

  /**
   * 🔴 浏览器 autoplay policy 要求 play() 在**用户手势内**发起，而真实试听音频要等后端
   * 克隆合成（秒级 ~ 数十秒），那时手势早已过期。所以：在 click 的**同一个 tick**里先对一个
   * 静音媒体同步发起 play()，把自动播放许可拿到手 —— 合成回来的真音频再 play() 就不会被拦。
   * play() 的 promise 一律 catch：绝不留 unhandled rejection。
   */
  const primeAutoplay = React.useCallback(() => {
    const el = primerRef.current;
    if (!el) return;
    try {
      el.currentTime = 0;
      void el.play().catch(() => {
        /* 解锁失败不打扰用户：真播放被拦时另有明确提示兜底 */
      });
    } catch {
      /* 个别浏览器 play() 可能同步抛错，同样不阻塞试听主流程 */
    }
  }, []);

  /** 播放某一行已合成好的试听样音（调用点：用户手势内 或 合成完成的 effect） */
  const playPreview = React.useCallback((name: string) => {
    const el = audioRefs.current[name];
    if (!el) return;
    try {
      el.currentTime = 0;
      void el.play().catch(() => {
        // ⭐ 不静默：明确告诉用户播放被浏览器拦了，以及怎么立刻听到
        const msg = tf(
          'audio.voicePreviewAutoplayBlocked',
          '浏览器阻止了自动播放：请点击播放器上的播放键，或再点一次「试听」'
        );
        patchPreview(name, { notice: msg });
        toastRef.current.error(msg);
      });
    } catch (e) {
      const msg = e instanceof Error && e.message ? e.message : tf('audio.voicePreviewFailed', '试听失败');
      patchPreview(name, { error: msg });
      toastRef.current.error(msg);
    }
  }, [patchPreview, tf]);

  /** 丢弃某行的试听（解绑 / 重传后旧样音不再代表当前音色）：停止播放并清状态 */
  const clearPreview = React.useCallback((name: string) => {
    const el = audioRefs.current[name];
    if (el) {
      try {
        el.pause();
        el.currentTime = 0;
      } catch {
        /* 元素可能已卸载 */
      }
    }
    delete autoPlayedRef.current[name];
    setPreviews((prev) => {
      if (!(name in prev)) return prev;
      const next = { ...prev };
      delete next[name];
      return next;
    });
  }, []);

  const load = React.useCallback(async () => {
    try {
      const d = await ttsApi.voiceBank(projectKey);
      setItems(d.items || []);
      setCloneOk(!!d.clone_available);
    } catch {
      /* 列表失败不打断音色管理主流程 */
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [projectKey]);

  React.useEffect(() => { load(); }, [load]);

  // 资产角色名（best-effort：取不到就用已绑定角色兜底，绝不让面板空白）
  React.useEffect(() => {
    let alive = true;
    charactersApi.list(projectKey)
      .then((chars) => {
        if (alive) setAssetChars(chars.map((c) => c.name).filter(Boolean));
      })
      .catch(() => { if (alive) setAssetChars([]); });
    return () => { alive = false; };
  }, [projectKey]);

  const boundOf = (name: string) => items.find((x) => x.character === name);

  // 候选角色 = 资产角色 ∪ 已绑定的角色（资产还没生成时也要能看到/管理已有绑定）
  const names = React.useMemo(() => {
    const s = new Set<string>(assetChars);
    items.forEach((x) => s.add(x.character));
    return Array.from(s).filter(Boolean);
  }, [assetChars, items]);

  const pickFile = (name: string) => {
    const el = document.createElement('input');
    el.type = 'file';
    el.accept = '.wav,.mp3,.flac,.m4a,.ogg,.aac';
    el.onchange = async () => {
      const f = el.files?.[0];
      if (!f) return;
      setBusyChar(name);
      try {
        const r = await ttsApi.voiceBankUpload({
          project_name: projectKey,
          character: name,
          file: f,
        });
        toast.success(r.message || t('audio.voiceBound'));
        // 换过参考音频：旧试听样音已不代表当前音色，作废（下次试听会重新合成）
        clearPreview(name);
        await load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : t('audio.voiceUploadFailed'));
      } finally {
        setBusyChar('');
      }
    };
    el.click();
  };

  /**
   * 试听（点一下 = 合成 + **自动播放** + 播放器就地展开在该行）：
   *  ① 本体**不是 async**：所有"必须在用户手势内"的动作（解锁自动播放、重播已有样音）
   *     都在 click 的同一个 tick 里同步跑完，绝不放到 await 之后；
   *  ② 已有合成结果的角色再点一次 → 同步重播（0 等待，也不再打后端）；
   *  ③ 首次 / 重新合成 → 行内立刻出现「正在合成…」，完成后自动播放。
   */
  const doPreview = (name: string) => {
    primeAutoplay(); // ← 同步：用户手势内解锁自动播放
    const cur = previews[name];
    if (cur?.url && audioRefs.current[name]) {
      patchPreview(name, { error: undefined, notice: undefined });
      playPreview(name);
      return;
    }
    if (cur?.loading) return; // 已有一次合成在跑：不重复发请求
    patchPreview(name, { loading: true, error: undefined, notice: undefined });
    ttsApi
      .voiceBankPreview({ project_name: projectKey, character: name })
      .then((r) => {
        if (!r?.url) throw new Error(tf('audio.voicePreviewFailed', '试听失败'));
        // 落到状态后由下面的 effect 立刻 play()（此时 <audio> 才刚挂载）
        patchPreview(name, { url: r.url, loading: false, error: undefined });
        toast.success(t('audio.voicePreviewReady'));
      })
      .catch((e) => {
        const msg = e instanceof Error && e.message ? e.message : tf('audio.voicePreviewFailed', '试听失败');
        patchPreview(name, { loading: false, error: msg });
        toastRef.current.error(msg);
      });
  };

  /**
   * 合成完成即自动播放。放在 effect 里是因为 <audio> 要等本次渲染挂载后才有 ref；
   * autoPlayedRef 保证同一个 url 只自动播放一次（play() 被拦时也不会反复重试刷屏）。
   */
  React.useEffect(() => {
    Object.entries(previews).forEach(([name, pv]) => {
      if (!pv.url || pv.loading) return;
      if (autoPlayedRef.current[name] === pv.url) return;
      autoPlayedRef.current[name] = pv.url;
      playPreview(name);
    });
  }, [previews, playPreview]);

  const doDelete = async (name: string) => {
    setBusyChar(name);
    try {
      const r = await ttsApi.voiceBankDelete(projectKey, name);
      toast.success(r.message || t('audio.voiceUnbound'));
      clearPreview(name);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('audio.voiceDeleteFailed'));
    } finally {
      setBusyChar('');
    }
  };

  if (loading) {
    return <Loading size="md" label={t('common.loading')} />;
  }

  return (
    <div className="rounded-lg border border-line bg-surface-2 p-4">
      <h3 className="text-lg font-semibold text-ink-1 mb-1 flex items-center gap-1.5">
        <Mic className="h-5 w-5" />
        {t('audio.voiceTitle')}
      </h3>
      <p className="text-xs text-ink-2 mb-3">{t('audio.voiceHint')}</p>

      {!cloneOk && (
        <div className="mb-3 p-2 rounded bg-warning-subtle border border-warning/30 text-warning-strong text-xs">
          {t('audio.voiceCloneUnavailable')}
        </div>
      )}

      {names.length === 0 ? (
        <div className="text-sm text-ink-3">{t('audio.voiceNoCharacters')}</div>
      ) : (
        <div className="space-y-2">
          {names.map((name) => {
            const b = boundOf(name);
            const pv: PreviewState = previews[name] || {};
            const busy = busyChar === name || !!pv.loading;
            /** 禁用原因：即使当前 cloneOk=true 也一定给 tooltip，绝不出现「点了没反应」的静默禁用 */
            const disabledTip = !cloneOk
              ? tf('audio.voiceCloneUnavailable', '克隆不可用：请确认 ComfyUI 提供了参考音频克隆节点')
              : pv.loading
                ? tf('audio.voicePreviewCookingShort', '正在合成试听音频…')
                : t('audio.voicePreview');
            return (
              <div key={name} className="rounded border border-line bg-surface px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-ink-1 truncate">
                      {name}
                      {b && (
                        <span className="ml-2 text-xs font-normal text-success-strong">
                          {t('audio.voiceBoundTag', {
                            sec: b.duration_sec ? b.duration_sec.toFixed(1) : '?',
                          })}
                        </span>
                      )}
                    </div>
                    {b?.ref_text && (
                      <div className="text-xs text-ink-3 truncate">
                        {t('audio.voiceRefText')}{b.ref_text}
                      </div>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {b && (
                      <>
                        {/* title 挂按钮上，外层 span 再兜一层 —— 有的浏览器不给 disabled 按钮弹
                            tooltip，包一层才能保证「为什么点不了」一定看得见 */}
                        <span title={disabledTip} className="inline-flex">
                          <Button
                            variant="ghost"
                            onClick={() => doPreview(name)}
                            disabled={busy || !cloneOk}
                            loading={!!pv.loading}
                            title={disabledTip}
                          >
                            {pv.loading
                              ? tf('audio.voicePreviewCookingShort', '合成中…')
                              : t('audio.voicePreview')}
                          </Button>
                        </span>
                        <Button variant="ghost" onClick={() => doDelete(name)} disabled={busy}>
                          {t('audio.voiceUnbind')}
                        </Button>
                      </>
                    )}
                    <Button variant="secondary" onClick={() => pickFile(name)} disabled={busy}>
                      {busy ? t('common.loading') : b ? t('audio.voiceReplace') : t('audio.voiceUpload')}
                    </Button>
                  </div>
                </div>

                {/* ⭐ 播放器**就地展开在本行**（此前挂在列表最底部：不自动播放 + 可能在视口外，
                    这正是用户「点了没反应」的原因） */}
                {(pv.loading || pv.url || pv.error || pv.notice) && (
                  <div className="mt-2 pt-2 border-t border-line">
                    {pv.loading && (
                      <div
                        className="text-xs text-ink-2 flex items-center gap-1.5"
                        role="status"
                        aria-live="polite"
                      >
                        <span
                          className="inline-block h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-current border-t-transparent"
                          aria-hidden="true"
                        />
                        {tf('audio.voicePreviewCooking', '正在合成试听音频…（克隆合成通常几秒到数十秒）')}
                      </div>
                    )}
                    {pv.url && (
                      <div className="flex items-center gap-2">
                        <audio
                          ref={(el) => {
                            audioRefs.current[name] = el;
                          }}
                          src={pv.url}
                          controls
                          preload="auto"
                          className="h-9 min-w-0 flex-1"
                          onPlay={() => patchPreview(name, { playing: true, notice: undefined })}
                          onPause={() => patchPreview(name, { playing: false })}
                          onEnded={() => patchPreview(name, { playing: false })}
                          onError={() => {
                            const msg = tf(
                              'audio.voicePreviewPlaybackFailed',
                              '试听音频加载失败，请再点一次「试听」'
                            );
                            patchPreview(name, { playing: false, error: msg });
                            toastRef.current.error(msg);
                          }}
                        />
                        <span
                          className={`text-xs shrink-0 ${pv.playing ? 'text-success-strong' : 'text-ink-3'}`}
                          aria-live="polite"
                        >
                          {pv.playing
                            ? tf('audio.voicePreviewPlaying', '播放中…')
                            : tf('audio.voicePreviewPaused', '已暂停')}
                        </span>
                      </div>
                    )}
                    {pv.notice && (
                      <div className="mt-1 text-xs text-warning-strong" role="alert">
                        {pv.notice}
                      </div>
                    )}
                    {pv.error && (
                      <div className="mt-1 text-xs text-danger-strong" role="alert">
                        {pv.error}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* 自动播放解锁器：隐藏、静音、不占布局。浏览器要求 play() 在用户手势内发起，
          而试听音频要等后端合成，这里负责把手势内的那次 play() 拿到手（见 primeAutoplay）。 */}
      <audio
        ref={(el) => {
          primerRef.current = el;
        }}
        src={SILENT_WAV_DATA_URI}
        preload="auto"
        muted
        className="hidden"
        aria-hidden="true"
      />
    </div>
  );
}
