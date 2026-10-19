/** CharacterOutfitsSection —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { characterOutfits } from '@/api/client';
import { Button, Input } from '@/components/ui';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { useToast } from '@/components/ui/toast';
import type { CharacterOutfit } from '@/types';


export function CharacterOutfitsSection({ projectKey, character }: { projectKey: string; character: string }) {
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