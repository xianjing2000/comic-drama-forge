// ============================================
// 桌面版「检查更新」入口（顶栏按钮 + 弹窗）
// ============================================
// 职责：在 Electron 桌面壳的 Navbar 里提供一个手动更新入口：
//   1. 点按钮 -> 拉 GitHub 最新 release，比较当前版本；
//   2. 有新版 -> 弹窗展示版本信息，用户选「资源增量」或「整包」；
//   3. 资源增量 -> 主进程下载 zip + SHA256 校验 + 覆盖镜像 + 重启后端（前端自动重载）；
//   4. 整包 -> 主进程下载 portable exe + 校验 + 原生确认框 + bootstrapper 接管重启。
//
// 浏览器版 / PyInstaller 单文件版：hasDesktopUpdater() 为 false，本组件根本不渲染。
import React, { useCallback, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { useToast } from '@/components/ui/toast';
import { Modal, Button } from '@/components/ui';
import {
  getDesktopUpdater,
  type DesktopUpdateInfo,
  type DesktopUpdateApplyResult,
} from '@/desktop/updater';
import { sanitizeErrorMessage } from '@/utils/errorText';

type Phase = 'idle' | 'checking' | 'choosing' | 'applying-resource' | 'applying-full';

export function UpdateChecker() {
  const { t } = useApp();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [phase, setPhase] = useState<Phase>('idle');
  const [info, setInfo] = useState<DesktopUpdateInfo | null>(null);
  const [busyKind, setBusyKind] = useState<'resource' | 'full' | null>(null);
  const [doneMsg, setDoneMsg] = useState('');
  const [errMsg, setErrMsg] = useState('');

  const updater = getDesktopUpdater();
  const busy = busyKind !== null;

  const openAndCheck = useCallback(async () => {
    if (!updater || busy) return;
    setOpen(true);
    setPhase('checking');
    setInfo(null);
    setErrMsg('');
    setDoneMsg('');
    try {
      const res = await updater.check();
      if (!res.ok) {
        setPhase('idle');
        setErrMsg(res.error || t('updater.checkFailed'));
        return;
      }
      setInfo(res);
      setPhase(res.update ? 'choosing' : 'idle');
    } catch (e) {
      setPhase('idle');
      setErrMsg(String((e as { message?: string })?.message || e));
    }
  }, [updater, busy]);

  const applyResource = useCallback(async () => {
    if (!updater) return;
    setBusyKind('resource');
    setPhase('applying-resource');
    setErrMsg('');
    try {
      const r: DesktopUpdateApplyResult = await updater.applyResource();
      if (r.ok) {
        setDoneMsg(t('updater.resourceUpdated', { v: r.latest || info?.latest || t('updater.latestTag') }));
        toast.success(doneMsg || t('updater.resourceUpdatedShort'));
        setTimeout(() => window.location.reload(), 1200);
      } else {
        setErrMsg(r.error || t('updater.resourceFailed'));
        setBusyKind(null);
        setPhase(info?.update ? 'choosing' : 'idle');
      }
    } catch (e) {
      setErrMsg(String((e as { message?: string })?.message || e));
      setBusyKind(null);
      setPhase(info?.update ? 'choosing' : 'idle');
    }
  }, [updater, info, toast, doneMsg]);

  const applyFull = useCallback(async () => {
    if (!updater) return;
    setBusyKind('full');
    setPhase('applying-full');
    setErrMsg('');
    try {
      const r: DesktopUpdateApplyResult = await updater.applyFull();
      const deferred = !!(r && (r as { deferred?: boolean }).deferred);
      if (deferred) {
        setBusyKind(null);
        setPhase('choosing');
        toast.info(t('updater.fullDownloaded'));
        return;
      }
      if (r.ok) {
        setDoneMsg(t('updater.fullApplied'));
        toast.success(doneMsg);
        setTimeout(() => { window.location.replace('about:blank'); }, 1500);
      } else {
        setErrMsg(r.error || t('updater.fullFailed'));
        setBusyKind(null);
        setPhase(info?.update ? 'choosing' : 'idle');
      }
    } catch (e) {
      setErrMsg(String((e as { message?: string })?.message || e));
      setBusyKind(null);
      setPhase(info?.update ? 'choosing' : 'idle');
    }
  }, [updater, info, toast, doneMsg]);

  return (
    <>
      <button
        type="button"
        onClick={() => { void openAndCheck(); }}
        title={t('updater.checkTitle')}
        aria-label={t('updater.checkLabel')}
        disabled={busy}
        className="flex shrink-0 items-center gap-1.5 rounded-md border border-line bg-surface-2/60 px-2.5 py-1.5 text-xs font-medium text-ink-2 transition-all duration-200 hover:border-brand/40 hover:text-brand hover:shadow-md disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
      >
        <svg className="h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12a9 9 0 1 1-2.64-6.36" />
          <path d="M21 3v6h-6" />
        </svg>
        <span className="hidden sm:inline">{busy ? t('updater.updating') : t('updater.checkLabel')}</span>
      </button>

      <Modal
        isOpen={open}
        onClose={() => { if (!busy) setOpen(false); }}
        title={t('updater.dialogTitle')}
        description={t('updater.dialogDesc')}
        size="md"
        preventClose={busy}
        footer={
          phase === 'choosing' ? (
            <div className="flex w-full items-center justify-end gap-2">
              <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>{t('common.close')}</Button>
              {info?.needResource && (
                <Button variant="secondary" onClick={() => void applyResource()} disabled={busy}>{t('updater.resourceBtn')}</Button>
              )}
              {info?.needFull && (
                <Button variant="primary" onClick={() => void applyFull()} disabled={busy}>{t('updater.fullBtn')}</Button>
              )}
            </div>
          ) : (
            <div className="flex w-full items-center justify-end gap-2">
              <Button variant="ghost" onClick={() => { setOpen(false); setPhase('idle'); }} disabled={busy}>
                {busy ? t('common.loading') : t('common.close')}
              </Button>
            </div>
          )
        }
      >
        <div className="space-y-3 text-sm">
          {phase === 'checking' && (
            <div className="flex items-center gap-2 text-ink-2">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-brand" />
              {t('updater.checkingGithub')}
            </div>
          )}

          {phase === 'choosing' && info && (
            <>
              <div className="rounded-lg border border-line bg-surface-2/40 p-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-ink-2">{t('updater.currentVersion')}</span>
                  <span className="font-mono text-ink-1">{info.current || '—'}</span>
                </div>
                <div className="mt-1 flex items-center justify-between gap-2">
                  <span className="text-ink-2">{t('updater.latestVersion')}</span>
                  <span className="font-mono text-brand">{info.latest || '—'}</span>
                </div>
                {info.releaseName && <p className="mt-2 text-xs text-ink-3">{info.releaseName}</p>}
              </div>
              <div className="text-xs leading-relaxed text-ink-2">
                <p><b>{t('updater.resourceDescTitle')}</b>：{t('updater.resourceDescBody')}</p>
                <p className="mt-1"><b>{t('updater.fullDescTitle')}</b>：{t('updater.fullDescBody')}</p>
                <p className="mt-1 text-ink-3">{t('updater.shaNote')}</p>
              </div>
            </>
          )}

          {(phase === 'applying-resource' || phase === 'applying-full') && (
            <div className="flex items-center gap-2 text-ink-2">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-line border-t-brand" />
              {phase === 'applying-resource'
                ? t('updater.downloadingResource')
                : t('updater.downloadingFull')}
            </div>
          )}

          {doneMsg && (
            <div className="rounded-md border border-success/30 bg-success-subtle p-3 text-xs text-success-strong">
              {doneMsg}
            </div>
          )}
          {errMsg && (
            <div className="rounded-md border border-danger/30 bg-danger-subtle p-3 text-xs text-danger">
              {sanitizeErrorMessage(errMsg)}
            </div>
          )}

          {phase === 'idle' && info && !info.update && !errMsg && (
            <div className="rounded-md border border-line bg-surface-2/40 p-3 text-xs text-ink-2">
              {t('updater.latestAlready', { v: info.current ?? '' })}
            </div>
          )}
        </div>
      </Modal>
    </>
  );
}
