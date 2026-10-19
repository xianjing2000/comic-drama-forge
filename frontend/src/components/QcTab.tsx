/** QcTab —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { qcApi } from '@/api/client';
import { Button, EmptyState, ErrorState, Modal, Skeleton } from '@/components/ui';
import { ClipboardCheck } from '@/components/ui/icons';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { describeError, sanitizeErrorMessage } from '@/utils/errorText';
import { useToast } from '@/components/ui/toast';

export function QcTab({ projectKey }: { projectKey: string }) {
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
  // t27（F6）：错误条此前只有一句文案 —— 既没有后端错误码，也没有重试入口。
  // 这里把 code 一起记下来（用于展示与报障），失败路径统一走下面的 failWith。
  const [errCode, setErrCode] = useState('');

  /** 记录一次失败：文案（已脱敏）+ 后端错误码，并返回文案供 toast 复用 */
  const failWith = (e: unknown, fallback: string): string => {
    const info = describeError(e, fallback);
    setError(info.message);
    setErrCode(info.code);
    return info.message;
  };

  // refreshDraft=true：用服务端配置重建草稿（首次加载 / 手动刷新 / 保存与端点动作之后）
  const load = async (refreshDraft = false) => {
    if (!projectKey) return;
    setLoading(true);
    setError('');
    setErrCode('');
    try {
      const resp: any = await qcApi.history(projectKey);
      setData(resp);
      const next = resp?.config ? { ...resp.config } : null;
      setCfgDraft((prev: any) => (refreshDraft || !prev ? next : prev));
    } catch (e) {
      failWith(e, t('qc.loadFailed'));
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
      failWith(e, t('qc.retestFailed'));
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
      const msg = failWith(e, t('qc.cfgSaveFailed'));
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
      const msg = failWith(e, t('qc.syncFailed'));
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
      const msg = failWith(e, t('qc.resetFailed'));
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
      const msg = failWith(e, t('qc.clearFailed'));
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
        description={sanitizeErrorMessage(error)}
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
        /* t27（F6）：软失败（页面已有数据，只是刷新/动作失败）→ 保留行内条形态，
           但补上后端错误码与「重试」入口，并统一脱敏（后端异常原文可能带本机路径）。 */
        <div className="flex items-start gap-3 p-3 bg-danger-subtle border border-danger/30 rounded-lg text-sm text-danger-strong">
          <div className="min-w-0 flex-1">
            <span>{sanitizeErrorMessage(error)}</span>
            {errCode && (
              <code className="ml-2 rounded-sm bg-surface-2 px-1.5 py-0.5 text-xs text-ink-3">{errCode}</code>
            )}
          </div>
          <Button size="sm" variant="secondary" onClick={() => void load()}>{t('common.retry')}</Button>
        </div>
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
          action={(
            <Button size="sm" variant="secondary" onClick={() => { void load(); }}>
              {t('common.refresh')}
            </Button>
          )}
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