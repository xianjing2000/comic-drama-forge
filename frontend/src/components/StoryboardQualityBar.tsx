/** 分镜质量三类汇总条（E-5 2026-10-16 从 StoryboardTab 抽出的展示型组件）。 */
import { useApp } from '@/context/AppContext';
import { FOCUS_RING } from '@/components/ui/focusRing';

export function StoryboardQualityBar({ quality, softOpen, onToggleSoft }: {
  quality: any; softOpen: boolean; onToggleSoft: () => void;
}) {
  const { t } = useApp();
  return (
        <div
          data-testid="sb-quality-bar"
          className={`rounded-lg border p-3 text-sm ${
            quality.critical > 0
              ? 'bg-danger-subtle border-danger/40 text-danger-strong'
              : quality.hasSoft
                ? 'bg-warning-subtle border-warning/40 text-warning-strong'
                : 'bg-success-subtle border-success/30 text-success-strong'
          }`}
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="font-medium">{t('sb.quality.title')}</span>
            <span>{t('sb.quality.success', { n: quality.success })}</span>
            {quality.hasSoft && (
              <span className="font-medium">
                {t('sb.quality.soft', { n: quality.soft, pct: Math.round(quality.softRatio * 100) })}
              </span>
            )}
            {quality.blocked > 0 && <span>{t('sb.quality.blocked', { n: quality.blocked })}</span>}
            {quality.pending > 0 && <span>{t('sb.quality.pending', { n: quality.pending })}</span>}
            {quality.critical > 0 && (
              <span className="font-semibold">
                {t('sb.quality.criticalNote', { n: quality.critical })}
              </span>
            )}
            {quality.hasSoft && (
              <button
                type="button"
                className={`ml-auto rounded px-2 py-1 text-xs underline ${FOCUS_RING}`}
                onClick={() => onToggleSoft()}
                aria-expanded={softOpen}
              >
                {softOpen ? t('sb.quality.hideList') : t('sb.quality.showList', { n: quality.soft })}
              </button>
            )}
          </div>
          {quality.hasSoft && (
            <p className="mt-1 text-xs opacity-90">{t('sb.quality.softHint')}</p>
          )}
          {softOpen && quality.softShots.length > 0 && (
            <ul className="mt-2 space-y-1 text-xs">
              {quality.softShots.map((s: any) => (
                <li key={`${s.shotId}-${s.seq}`} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-mono">#{s.seq}</span>
                  <span className="min-w-0 flex-1">{s.reason || t('sb.quality.softNoReason')}</span>
                  <span className={s.critical.length ? 'font-medium' : 'opacity-80'}>
                    {s.critical.length
                      ? t('sb.quality.criticalCount', { n: s.critical.length })
                      : t('sb.quality.noCritical')}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
  );
}
