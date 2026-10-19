/** AssetCard —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { useState } from 'react';
import { useApp } from '@/context/AppContext';
import { Box, Mountain, User } from '@/components/ui/icons';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { assetSrc, type AssetItem } from '@/pages/workbenchShared';


export function AssetCard({
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