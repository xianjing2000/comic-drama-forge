/** 提示条（E-5 从 StoryboardTab 抽出的展示型组件）。 */
import { useApp } from '@/context/AppContext';
import { Button } from '@/components/ui';

export function StoryboardNoticeBar({ notice, canGotoOutput, onGotoOutput }: {
  notice: string; canGotoOutput: boolean; onGotoOutput?: () => void;
}) {
  const { t } = useApp();
  return (
    <>
      {notice && (
        <div className="flex items-start gap-3 p-3 bg-success-subtle border border-success/30 rounded-lg text-success-strong text-sm">
          <span className="min-w-0 flex-1">{notice}</span>
          {/* t27（F5）：失效提示必须带出口 —— 否则用户知道「要重新合成」却找不到入口 */}
          {canGotoOutput && onGotoOutput && (
            <Button size="sm" variant="secondary" onClick={onGotoOutput}>
              {t('sb.gotoOutputReview')}
            </Button>
          )}
        </div>
      )}
    </>
  );
}
