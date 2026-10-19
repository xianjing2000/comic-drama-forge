/** 资产展示区（E-5 2026-10-16 从 OverviewTab 抽出的展示型组件；含 groups/total 派生）。 */
import React from 'react';
import { useApp } from '@/context/AppContext';
import { Button } from '@/components/ui';
import { User, Box, Mountain, Upload } from '@/components/ui/icons';
import { AssetCard } from '@/components/AssetCard';
import { AssetPreviewModal } from '@/components/AssetPreviewModal';
import type { AssetItem, ProjectAssets } from '@/pages/workbenchShared';

export function AssetsGallery({ assets, preview, setPreview, projectKey, onRefreshAssets, onUpload }: {
  assets: ProjectAssets | null;
  preview: { item: AssetItem; type: 'character' | 'item' | 'scene' } | null;
  setPreview: (v: any) => void;
  projectKey: string; onRefreshAssets: () => void; onUpload: () => void;
}) {
  const { t } = useApp();
  const groups: { key: 'characters' | 'items' | 'scenes'; label: string; icon: React.ReactNode; type: 'character' | 'item' | 'scene' }[] = [
    { key: 'characters', label: t('wb.characters'), icon: <User className="h-4 w-4" />, type: 'character' },
    { key: 'items', label: t('wb.items'), icon: <Box className="h-4 w-4" />, type: 'item' },
    { key: 'scenes', label: t('wb.scenes'), icon: <Mountain className="h-4 w-4" />, type: 'scene' },
  ];
  const total = groups.reduce((n, g) => n + (assets?.gallery?.[g.key]?.length || 0), 0);
  return (
    <>
      {/* 资产展示 */}
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-lg font-semibold text-ink-1">{t('wb.assetsTitle')}</h3>
        <Button variant="secondary" onClick={() => onUpload()}>
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
    </>
  );
}
