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
  const [previewUrl, setPreviewUrl] = React.useState('');
  /** 资产角色名（优先候选）：资产库角色 = 剧本角色，供「未绑定也能先传」 */
  const [assetChars, setAssetChars] = React.useState<string[]>([]);

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
        await load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : t('audio.voiceUploadFailed'));
      } finally {
        setBusyChar('');
      }
    };
    el.click();
  };

  const doPreview = async (name: string) => {
    setBusyChar(name);
    try {
      const r = await ttsApi.voiceBankPreview({ project_name: projectKey, character: name });
      setPreviewUrl(r.url);
      toast.success(t('audio.voicePreviewReady'));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('audio.voicePreviewFailed'));
    } finally {
      setBusyChar('');
    }
  };

  const doDelete = async (name: string) => {
    setBusyChar(name);
    try {
      const r = await ttsApi.voiceBankDelete(projectKey, name);
      toast.success(r.message || t('audio.voiceUnbound'));
      setPreviewUrl('');
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
            const busy = busyChar === name;
            return (
              <div
                key={name}
                className="flex items-center justify-between gap-2 rounded border border-line bg-surface px-3 py-2"
              >
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
                      <Button
                        variant="ghost"
                        onClick={() => doPreview(name)}
                        disabled={busy || !cloneOk}
                      >
                        {t('audio.voicePreview')}
                      </Button>
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
            );
          })}
        </div>
      )}

      {previewUrl && (
        <div className="mt-3">
          <div className="text-xs text-ink-2 mb-1">{t('audio.voicePreviewLabel')}</div>
          <audio controls src={previewUrl} className="w-full h-9" />
        </div>
      )}
    </div>
  );
}
