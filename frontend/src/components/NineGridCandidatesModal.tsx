/** 九宫格候选构图弹窗（E-5 2026-10-16 从 StoryboardTab 抽出的**展示型**组件，逻辑仍在父组件）。 */
import { Modal, Button, Loading } from '@/components/ui';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { useApp } from '@/context/AppContext';

export function NineGridCandidatesModal({ open, seq, phase, imgUrl, cell, error,
  onClose, onApply, onRetry, onPickCell }: {
  open: boolean; seq: number | null;
  phase: 'generating' | 'ready' | 'applying';
  imgUrl: string; cell: number | null; error: string;
  onClose: () => void; onApply: () => void; onRetry: () => void; onPickCell: (n: number) => void;
}) {
  const { t } = useApp();
  return (
    <>
      {/* 九宫格候选构图弹窗：生成中显示进度提示；完成后在候选图上按 3x3 等分覆盖 9 个
          透明选格按钮（行优先 1-9，与后端 crop_grid_cell 的整图等分切分逐格对齐），
          点格 → 「应用所选格」即裁切为该镜正式分镜图并刷新画布 */}
      <Modal
        isOpen={open}
        onClose={onClose}
        preventClose={phase === 'applying'}
        title={t('sb.grid.modalTitle', { seq: seq ?? '' })}
        description={t('sb.grid.pickHint')}
        size="lg"
        footer={
          <>
            <span className="mr-auto text-xs text-ink-2">
              {cell != null ? t('sb.grid.pickedCell', { n: cell }) : ''}
            </span>
            <Button variant="secondary" onClick={onClose} disabled={phase === 'applying'}>
              {t('common.close')}
            </Button>
            <Button
              onClick={onApply}
              disabled={phase !== 'ready' || cell == null || !!error || !imgUrl}
              loading={phase === 'applying'}
            >
              {phase === 'applying' ? t('sb.grid.applying') : t('sb.grid.apply')}
            </Button>
          </>
        }
      >
        {/* 生成中：ComfyUI 出图可能耗时一两分钟，给明确进度文案，防用户以为卡死 */}
        {phase === 'generating' && (
          <Loading size="md" label={t('sb.grid.generating')} />
        )}

        {/* 生成失败 / 取消：原因留在弹窗内，可原地重试（异常不打断页面） */}
        {phase !== 'generating' && error && (
          <div className="space-y-3">
            <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm break-all">
              {error}
            </div>
            <Button
              variant="secondary"
              onClick={() => onRetry()}
              disabled={phase === 'applying'}
            >
              {t('sb.grid.regenerate')}
            </Button>
          </div>
        )}

        {/* 候选图 + 3x3 选格覆盖层：划分与后端裁切同口径（整图等分三行三列，行优先） */}
        {phase !== 'generating' && !error && imgUrl && (
          <div className="relative select-none">
            <img
              src={imgUrl}
              alt={t('sb.grid.imageAlt', { seq: seq ?? '' })}
              className="block w-full rounded-md border border-line bg-surface-2"
            />
            <div className="absolute inset-0 grid grid-cols-3 grid-rows-3">
              {Array.from({ length: 9 }, (_, i) => i + 1).map((n) => (
                <button
                  key={n}
                  type="button"
                  aria-label={t('sb.grid.cellN', { n })}
                  aria-pressed={cell === n}
                  title={t('sb.grid.cellN', { n })}
                  onClick={() => onPickCell(n)}
                  className={`relative cursor-pointer transition-colors ${FOCUS_RING} ${
                    cell === n
                      ? 'border-2 border-brand bg-brand/25'
                      : 'border border-transparent hover:border-brand/70 hover:bg-brand/10'
                  }`}
                >
                  <span
                    className={`absolute left-1 top-1 rounded px-1.5 py-0.5 text-xs font-medium ${
                      cell === n ? 'bg-brand text-white' : 'bg-slate-900/70 text-white'
                    }`}
                  >
                    {n}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}
