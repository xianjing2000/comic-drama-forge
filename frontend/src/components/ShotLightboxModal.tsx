/** 分镜图放大 lightbox（E-5 2026-10-16 从 StoryboardTab 抽出的**展示型**组件）。 */
import { Modal } from '@/components/ui';
import { useApp } from '@/context/AppContext';

export function ShotLightboxModal({ img, onClose }: {
  img: { src: string; seq: number; grid?: boolean } | null;
  onClose: () => void;
}) {
  const { t } = useApp();
  return (
    <>
      {/* 分镜图点击放大（lightbox）：正式图 / 生成中 scratch 图都支持；原尺寸展示，ESC / 点遮罩关闭 */}
      <Modal
        isOpen={img !== null}
        onClose={onClose}
        title={t('sb.zoomTitle')}
        description={img ? `#${img.seq}` : ''}
        size="full"
      >
        {img && (
          <div className="relative mx-auto w-fit select-none">
            <img
              src={img.src}
              alt={t('sb.imageAlt', { seq: img.seq })}
              className="max-w-full max-h-[78vh] rounded-lg border border-line bg-surface-2 object-contain"
            />
            {/* 九宫格 1-9 编号覆盖层（2026-10-06）：编号由前端叠加，不再让模型画进图
                （扩散模型写数字实测乱码）。划分与后端裁切口径一致：整图等分三行三列。 */}
            {img.grid && (
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
    </>
  );
}
