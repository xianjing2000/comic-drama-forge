/** AssetPrecipitationSection —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';
import { assetPrecipitation, type AssetPrecipitationResponse, type PrecipitationStatus } from '@/api/client';
import { FOCUS_RING } from '@/components/ui/focusRing';


export function AssetPrecipitationSection({
  projectKey,
  kind,
  name,
}: {
  projectKey: string;
  kind: string;
  name: string;
}) {
  const { t } = useApp();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<AssetPrecipitationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    setData(null);
    setError(false);
    if (!open || !projectKey || !name) return;
    let alive = true;
    setLoading(true);
    assetPrecipitation
      .get(projectKey, kind, name)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setError(true); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [open, projectKey, kind, name]);

  // 状态 → 圆点样式 + 文案。skipped/pending 视觉上明显弱于 done/failed，
  // 因为它们是「本来就没有这一步」而非「出错了」，不该让用户误以为有问题。
  const dotOf = (status: PrecipitationStatus) => {
    switch (status) {
      case 'done':    return 'bg-success-strong border-success-strong';
      case 'failed':  return 'bg-danger-strong border-danger-strong';
      case 'pending': return 'bg-surface border-line-strong';
      default:        return 'bg-surface-2 border-line';
    }
  };
  const badgeOf = (status: PrecipitationStatus) => {
    switch (status) {
      case 'done':    return 'bg-success-subtle text-success-strong';
      case 'failed':  return 'bg-danger-subtle text-danger-strong';
      default:        return 'bg-surface-2 text-ink-3';
    }
  };
  const statusText = (status: PrecipitationStatus) =>
    t(`precip.status.${status}`);

  const summary = data?.summary;

  return (
    <div className="border border-line rounded-lg p-3 space-y-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`flex w-full items-center justify-between rounded text-sm font-semibold text-ink-1 hover:text-brand transition-colors ${FOCUS_RING}`}
      >
        <span className="flex items-center gap-2">
          <span>{t('precip.title')}</span>
          {summary && (
            <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs font-normal text-ink-3">
              {t('precip.summary.done', { done: summary.done, total: summary.total_steps })}
            </span>
          )}
        </span>
        <span className="text-xs text-ink-3">{open ? '−' : '+'}</span>
      </button>

      {open && (
        <div className="space-y-3">
          {loading ? (
            <div className="text-xs text-ink-3">{t('common.loading')}</div>
          ) : error || !data ? (
            <div className="text-xs text-ink-3">{t('precip.loadFailed')}</div>
          ) : (
            <>
              {/* 概览：质检次数 / 命中教训 / 视角数 / 是否用户上传 */}
              <div className="flex flex-wrap gap-1.5">
                {summary?.is_user_upload && (
                  <span className="rounded-full bg-brand-subtle px-2 py-0.5 text-xs font-medium text-brand-strong">
                    {t('precip.badge.userUpload')}
                  </span>
                )}
                <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-ink-2">
                  {t('precip.stat.qc', { n: summary?.qc_attempts ?? 0 })}
                </span>
                <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-ink-2">
                  {t('precip.stat.lessons', { n: summary?.lessons ?? 0 })}
                </span>
                {!!summary?.views?.length && (
                  <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs text-ink-2">
                    {t('precip.stat.views', { n: summary.views.length })}
                  </span>
                )}
              </div>

              {/* 步骤时间线 */}
              <ol className="space-y-0">
                {data.steps.map((s, i) => (
                  <li key={s.id} className="flex gap-2.5">
                    {/* 竖线 + 圆点 */}
                    <span className="flex flex-col items-center pt-1.5">
                      <span className={`h-2.5 w-2.5 shrink-0 rounded-full border-2 ${dotOf(s.status)}`} />
                      {i < data.steps.length - 1 && <span className="w-px flex-1 bg-line" />}
                    </span>
                    <div className="min-w-0 flex-1 pb-3">
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-ink-1">{s.label}</span>
                        <span
                          className={`shrink-0 rounded-full px-1.5 py-0.5 text-xs font-medium ${badgeOf(s.status)}`}
                        >
                          {statusText(s.status)}
                        </span>
                      </div>
                      {s.detail && (
                        <p className="mt-0.5 text-xs text-ink-3 break-words">{s.detail}</p>
                      )}

                      {/* 质检逐次尝试：哪一次、多少分、因为什么被打回 */}
                      {s.id === 'qc' && s.items.length > 0 && (
                        <ul className="mt-1.5 space-y-1">
                          {s.items.map((it, j) => (
                            <li
                              key={j}
                              className="rounded-md border border-line bg-surface-2 px-2 py-1.5 text-xs"
                            >
                              <div className="flex items-center gap-2">
                                <span className="text-ink-2">
                                  {t('precip.qc.attempt', { n: it.attempt ?? j + 1 })}
                                </span>
                                {it.score != null && (
                                  <span className="text-ink-3">
                                    {t('precip.qc.score', { score: it.score })}
                                  </span>
                                )}
                                <span className={it.passed ? 'text-success-strong' : 'text-danger-strong'}>
                                  {it.passed ? t('precip.qc.passed') : t('precip.qc.failed')}
                                </span>
                                {it.seed != null && (
                                  <span className="ml-auto text-ink-3">seed {it.seed}</span>
                                )}
                              </div>
                              {it.reason && (
                                <p className="mt-0.5 text-ink-3 break-words">{it.reason}</p>
                              )}
                              {!!it.issues?.length && (
                                <ul className="mt-0.5 list-disc pl-4 text-ink-3">
                                  {it.issues.map((x, k) => (
                                    <li key={k} className="break-words">{x}</li>
                                  ))}
                                </ul>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}

                      {/* 教训：下次重画时会被召回用于改写提示词 */}
                      {s.id === 'lesson' && s.items.length > 0 && (
                        <ul className="mt-1.5 space-y-1">
                          {s.items.map((it, j) => (
                            <li
                              key={j}
                              className="rounded-md border border-line bg-surface-2 px-2 py-1.5 text-xs"
                            >
                              <div className="flex items-start gap-2">
                                <span className="min-w-0 flex-1 text-ink-2 break-words">{it.issue}</span>
                                {it.category && (
                                  <span className="shrink-0 rounded-full bg-surface px-1.5 py-0.5 text-xs text-ink-3">
                                    {it.category}
                                  </span>
                                )}
                                {it.priority && (
                                  <span className="shrink-0 rounded-full bg-surface px-1.5 py-0.5 text-xs text-ink-3">
                                    {it.priority}
                                  </span>
                                )}
                              </div>
                              {it.project && (
                                <p className="mt-0.5 text-ink-3">
                                  {t('precip.lesson.from', { project: it.project })}
                                </p>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}

                      {/* 通用键值条目（抽取字段 / 提示词 / seed / 视角清单 / 目录） */}
                      {s.id !== 'qc' && s.id !== 'lesson' && s.items.filter((x) => x.key).length > 0 && (
                        <dl className="mt-1 space-y-0.5">
                          {s.items.filter((x) => x.key).map((it, j) => (
                            <div key={j} className="flex gap-2 text-xs">
                              <dt className="shrink-0 text-ink-3">{it.key}</dt>
                              <dd className="min-w-0 flex-1 text-ink-2 break-words whitespace-pre-wrap">
                                {it.value}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            </>
          )}
        </div>
      )}
    </div>
  );
}