/** 前置解析卡片（E-5 2026-10-16 从 OverviewTab 抽出的展示型组件）。 */
import { useApp } from '@/context/AppContext';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { X } from '@/components/ui/icons';
import type { ChapterPreflightResult } from '@/api/client';

export function ChapterPreflightCard({ open, detail, onClose }: {
  open: boolean; detail: ChapterPreflightResult | null; onClose: () => void;
}) {
  const { t } = useApp();
  return (
    <>
            {/* 前置解析卡片：人物档案 + 故事梗概 + 关键事件 + 情绪基线 */}
            {open && (
              <div className="bg-surface rounded-lg border border-line p-4 mb-4">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <h4 className="font-semibold text-ink-1">{t('wb.preflightTitle')}</h4>
                    <p className="text-xs text-ink-3 mt-0.5">{t('wb.preflightDesc')}</p>
                  </div>
                  <button onClick={() => onClose()}
                    className={`p-1.5 rounded-md text-ink-3 hover:bg-surface-2 transition-colors ${FOCUS_RING}`}>
                    <X className="h-4 w-4" />
                  </button>
                </div>
                {detail ? (
                  <div className="space-y-3">
                    <div className="text-sm text-ink-2">
                      <strong className="text-ink-1">{t('wb.preflightSummary')}</strong>
                      <p className="mt-1 text-ink-2">{detail.story_summary}</p>
                    </div>
                    {detail.key_events?.length > 0 && (
                      <div>
                        <strong className="text-sm text-ink-1">{t('wb.preflightKeyEvents')}</strong>
                        <ol className="mt-1 space-y-0.5 text-xs text-ink-2 list-decimal list-inside">
                          {detail.key_events.map((ev, i) => <li key={i}>{ev}</li>)}
                        </ol>
                      </div>
                    )}
                    {detail.characters?.length > 0 && (
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                        {detail.characters.map(c => (
                          <div key={c.name} className="p-2.5 rounded-lg bg-surface-2 border border-line">
                            <div className="flex items-center gap-1.5 mb-1">
                              <span className="text-sm font-medium text-ink-1">{c.name}</span>
                              {c.gender && <span className="text-xs text-ink-3">{c.gender}</span>}
                              {c.identity && <span className="text-xs text-ink-3">· {c.identity}</span>}
                            </div>
                            {c.personality && (
                              <p className="text-xs text-ink-2"><strong>{t('wb.preflightPersonality')}：</strong>{c.personality}</p>
                            )}
                            {c.emotions?.length > 0 && (
                              <p className="text-xs text-ink-2 mt-0.5">
                                <strong>{t('wb.preflightEmotion')}</strong>{' '}
                                {c.emotions.slice(0, 4).map(e => e.emotion).join(' / ')}
                              </p>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="text-xs text-ink-3">{t('wb.preflightNoData')}</div>
                )}
              </div>
            )}
    </>
  );
}
