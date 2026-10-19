/** 分集断点提议卡片（E-5 2026-10-16 从 OverviewTab 抽出的展示型组件）。 */
import { useApp } from '@/context/AppContext';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { sanitizeErrorMessage } from '@/utils/errorText';

export function SplitPlanCard({ open, loading, error, plan, onRetry }: {
  open: boolean; loading: boolean; error: string; plan: any; onRetry: () => void;
}) {
  const { t } = useApp();
  return (
    <>
              {/* P2-2 分集断点提议卡片（与生产口径同源的只读预览） */}
              {open && (
                <div className="px-4 py-4 border-b border-line bg-surface-2/50">
                  {loading && (
                    <div className="text-sm text-ink-2 flex items-center gap-2">
                      <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
                      {t('wb.loading')}
                    </div>
                  )}
                  {!loading && error && (
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-sm text-danger-strong">{sanitizeErrorMessage(error)}</span>
                      <button
                        onClick={onRetry}
                        className={`px-2.5 py-1 rounded-md text-xs bg-surface-2 text-ink-2 hover:bg-line ${FOCUS_RING}`}
                      >
                        {t('wb.planRetry')}
                      </button>
                    </div>
                  )}
                  {!loading && !error && plan && plan.chapters?.length > 0 && (
                    <div className="space-y-3">
                      {/* 汇总行 */}
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="font-medium text-ink-1">
                          {t('wb.planTotal', { ep: plan.total_episodes ?? 0, ch: plan.chapter_count ?? plan.chapters.length })}
                        </span>
                        {(plan.needs_confirm_chapters?.length ?? 0) > 0 && (
                          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-warning-subtle text-warning-strong">
                            {t('wb.planNeedsConfirm', { n: plan.needs_confirm_chapters.length })}
                          </span>
                        )}
                      </div>
                      {/* 逐章断点 */}
                      <div className="space-y-2">
                        {plan.chapters.map((ch: any) => (
                          <div key={ch.index} className="bg-surface rounded-lg border border-line p-3">
                            <div className="flex flex-wrap items-center gap-2 mb-2">
                              <span className="text-sm font-semibold text-ink-1">
                                {t('wb.planChapter', { n: ch.index })}
                                {ch.title && <span className="ml-1.5 text-sm text-brand font-normal">{t('wb.planChapterTitle', { title: ch.title })}</span>}
                              </span>
                              <span className="text-xs text-ink-2">{t('wb.planParts', { n: ch.total_parts })}</span>
                              {ch.needs_confirm && (
                                <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-warning-subtle text-warning-strong">
                                  {t('wb.planConfirmNote')}
                                </span>
                              )}
                            </div>
                            {ch.units?.length > 0 && (
                              <div className="space-y-1">
                                {ch.units.map((u: any) => (
                                  <div key={u.part} className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
                                    <span className="font-medium text-ink-1 w-24 shrink-0">
                                      {t('wb.planPart', { part: u.part, total: ch.total_parts })}
                                    </span>
                                    <span className="tabular-nums">{t('wb.planShots', { n: u.est_shots ?? '—' })}</span>
                                    <span className="tabular-nums">{t('wb.planSec', { sec: u.est_sec ?? '—' })}</span>
                                    {u.over_redline && (
                                      <span className="px-1.5 py-0.5 rounded text-xs font-medium bg-danger-subtle text-danger-strong">
                                        {t('wb.planOverRedline')}
                                      </span>
                                    )}
                                    {u.preview && (
                                      <span className="text-ink-3 truncate max-w-[14rem]">{u.preview}</span>
                                    )}
                                  </div>
                                ))}
                              </div>
                            )}
                            {ch.message && (
                              <p className="mt-1.5 text-xs text-ink-3">{ch.message}</p>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  {!loading && !error && plan && (plan.chapters?.length ?? 0) === 0 && (
                    <p className="text-sm text-ink-3">{t('wb.planNoChapters')}</p>
                  )}
                </div>
              )}
    </>
  );
}
