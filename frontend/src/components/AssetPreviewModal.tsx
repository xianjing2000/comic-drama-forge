/** AssetPreviewModal —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { assetsApi, asOverwriteConfirm, overwriteScopeSummary, type OverwriteConfirmPayload } from '@/api/client';
import { Button, ConfirmDialog, EmptyState, Modal } from '@/components/ui';
import { ImageIcon } from '@/components/ui/icons';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { useToast } from '@/components/ui/toast';
import { assetSrc, type AssetItem } from '@/pages/workbenchShared';
import { AssetPrecipitationSection } from '@/components/AssetPrecipitationSection';
import { CharacterOutfitsSection } from '@/components/CharacterOutfitsSection';


export function AssetPreviewModal({
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
  /** 该资产的落盘质检结论（base.meta.json 的 qc，后端 asset-detail 下发）：
   *  {accept,label,reason,critical_issues} —— 弹窗里显示「这张图质检过没过、问题在哪」。 */
  const [assetQc, setAssetQc] = useState<Record<string, any> | null>(null);
  /** 后端 409「跨集覆盖未确认」的影响范围（非空 = 确认框打开） */
  const [overwriteConfirm, setOverwriteConfirm] = useState<OverwriteConfirmPayload | null>(null);
  const [confirming, setConfirming] = useState(false);

  /** 本轮新增文案：locales 不在本任务范围（captain 统一补），缺 key 时回落中文，绝不显示裸 key */
  const tf = (key: string, fallback: string, params?: Record<string, string | number>) => {
    const v = t(key, params);
    if (v !== key) return v;
    let out = fallback;
    if (params) {
      Object.entries(params).forEach(([k, val]) => {
        out = out.replace(new RegExp(`\\{${k}\\}`, 'g'), String(val));
      });
    }
    return out;
  };
  const confirmSummary = overwriteConfirm ? overwriteScopeSummary(overwriteConfirm) : null;

  useEffect(() => {
    setActive(0);
    setPromptZh('');
    setPromptEn('');
    setAssetQc(null);
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
        setAssetQc(d?.meta?.qc || null);
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

  /** 组装 /api/assets/generate 请求体（confirmOverwrite 只在用户确认框点头后为 true） */
  const buildGenerateBody = (confirmOverwrite: boolean) => ({
    asset_type: preview?.type || 'character',
    project_name: projectKey,
    overwrite: true,
    ...(confirmOverwrite ? { confirm_overwrite: true } : {}),
    assets: [{
      name: item?.name,
      reference_prompt_zh: promptZh.trim(),
      reference_prompt_en: promptEn.trim(),
    }],
  });

  // 单点重新生成：用当前编辑后的提示词覆盖重新出图（overwrite=true）。
  // ⚠️ 角色设定图是**项目级共用基准**（assets/characters/<项目>/<角色>/base.png），
  //    跨集覆盖时后端会先回 409（t16）；这里把它变成确认框，
  //    用户点头后才带 confirm_overwrite 重发同一请求。
  const regenerate = async () => {
    if (!preview || regenerating) return;
    const zh = promptZh.trim();
    if (!zh) {
      toast.warning(t('wb.assetPromptRequired'));
      return;
    }
    setRegenerating(true);
    try {
      const d = await assetsApi.generate(buildGenerateBody(false));
      if (!d.task_id) {
        toast.error(d?.error || t('wb.assetRegenerateFailed'));
      } else {
        toast.success(t('wb.assetRegenerateStarted'));
        onRegenerated?.();
      }
    } catch (e) {
      // 判定看后端 code/needs_confirm（**不按 HTTP 状态码**）：跨集覆盖 → 弹确认框；
      // 其余错误照旧提示失败 —— 行为与改动前逐字一致（零回归）。
      const info = asOverwriteConfirm(e);
      if (info) {
        setOverwriteConfirm(info);
        return;
      }
      toast.error(e instanceof Error ? e.message : t('wb.assetRegenerateFailed'));
    } finally {
      setRegenerating(false);
    }
  };

  /** 确认框点「确认覆盖」→ 带 confirm_overwrite=true 重发同一请求 */
  const confirmOverwriteRegenerate = async () => {
    setConfirming(true);
    try {
      const d = await assetsApi.generate(buildGenerateBody(true));
      if (!d.task_id) {
        toast.error(d?.error || t('wb.assetRegenerateFailed'));
      } else {
        toast.success(t('wb.assetRegenerateStarted'));
        // 后端 200 时回显的覆盖影响范围（另有一份写进日志）
        if (d.overwrite_warning) toast.info(d.overwrite_warning);
        onRegenerated?.();
      }
      setOverwriteConfirm(null);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('wb.assetRegenerateFailed'));
    } finally {
      setConfirming(false);
    }
  };

  return (
    <>
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
            <EmptyState
              icon={<ImageIcon className="h-10 w-10" />}
              title={t('wb.imageUnavailable')}
              action={(
                <Button size="sm" variant="secondary" onClick={onClose}>
                  {t('common.close')}
                </Button>
              )}
            />
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

          {/* ⭐ 2026-10-17：质检结论 —— 让用户一眼看到「这张图质检过没过、问题在哪」 */}
          {assetQc && (() => {
            const acc = assetQc.accept === true;
            const label = String(assetQc.label || (acc ? tf('wb.assetQcPass', '达标') : tf('wb.assetQcFailed', '未达标')));
            const reason = String(assetQc.reason || '');
            const crit: string[] = Array.isArray(assetQc.critical_issues)
              ? assetQc.critical_issues.map((x: any) => String(x || '').trim()).filter(Boolean)
              : [];
            return (
              <div className={`border rounded-lg p-3 space-y-1.5 ${
                acc ? 'border-success/40 bg-success-subtle' : 'border-danger/40 bg-danger-subtle'
              }`}>
                <h4 className={`text-sm font-semibold ${acc ? 'text-success-strong' : 'text-danger-strong'}`}>
                  {tf('wb.assetQcTitle', '质检结论')}：{label}
                </h4>
                {reason && <p className={`text-xs ${acc ? 'text-ink-2' : 'text-danger-strong'}`}>{reason}</p>}
                {crit.length > 0 && (
                  <ul className="list-inside list-disc space-y-0.5 text-xs text-danger-strong">
                    {crit.map((c, i) => (<li key={i}>{c}</li>))}
                  </ul>
                )}
              </div>
            );
          })()}

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

    {/* ⭐ 跨集覆盖确认框：如实展示后端 409 给出的影响范围（集号 + 资产数 + 逐资产），
        点头后才带 confirm_overwrite=true 重发；取消则什么都不做（不发任何请求）。 */}
      <ConfirmDialog
        isOpen={!!overwriteConfirm}
        onClose={() => setOverwriteConfirm(null)}
        onConfirm={confirmOverwriteRegenerate}
        loading={confirming}
        danger
        title={tf('wb.overwriteConfirmTitle', '这次重生成会改写多集的人物形象，确认？')}
        confirmText={tf('wb.overwriteConfirmOk', '确认覆盖')}
        cancelText={t('common.cancel')}
        message={
          confirmSummary ? (
            <div className="space-y-2">
              <p>
                {tf(
                  'wb.overwriteConfirmBody',
                  '这只角色会影响第 {eps} 集（共 {n} 个资产）—— 确认要覆盖吗？',
                  { eps: confirmSummary.episodeText, n: confirmSummary.assetCount }
                )}
              </p>
              {confirmSummary.assets.length > 0 && (
                <ul className="list-inside list-disc space-y-0.5 text-xs text-ink-3">
                  {confirmSummary.assets.map((a) => (
                    <li key={a.name}>
                      {tf('wb.overwriteConfirmRow', '{name}：第 {eps} 集', {
                        name: a.name,
                        eps: a.episodeText,
                      })}
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-xs text-danger-strong">
                {tf(
                  'wb.overwriteConfirmWarn',
                  '确认后会立即改写该角色在所有已出现集里的形象基准（含第 1 集），且不可撤销。'
                )}
              </p>
            </div>
          ) : null
        }
      />
    </>
  );
}