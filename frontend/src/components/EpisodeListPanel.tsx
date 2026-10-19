/** 剧集列表卡（E-5 2026-10-16 从 OverviewTab 抽出的展示型组件；含分集断点提议 + 场次子列表）。 */
import React from 'react';
import { useApp } from '@/context/AppContext';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { Check, X } from '@/components/ui/icons';
import { SplitPlanCard } from '@/components/SplitPlanCard';

export function EpisodeListPanel({ episodes, splitPlanOpen, setSplitPlanOpen, fetchSplitPlan,
  splitPlanLoading, splitPlanError, splitPlan, preflightOpen, setPreflightOpen,
  loadEpisodeDetail, toggleScenes, runPreflight, openPreflightDetail,
  scenesOpenEps, scenesByEp, scenesLoadingEp, scenesErrByEp, fetchScenes,
  redoScene, redoBusy, preflightDone, preflightRunning }: {
  episodes: any[];
  splitPlanOpen: boolean; setSplitPlanOpen: React.Dispatch<React.SetStateAction<boolean>>; fetchSplitPlan: () => void;
  splitPlanLoading: boolean; splitPlanError: string; splitPlan: any;
  preflightOpen: boolean; setPreflightOpen: React.Dispatch<React.SetStateAction<boolean>>;
  loadEpisodeDetail: (n: number) => void; toggleScenes: (n: number) => void;
  runPreflight: (n: number) => void; openPreflightDetail: (n: number) => void;
  scenesOpenEps: number[]; scenesByEp: Record<number, any>; scenesLoadingEp: number | null;
  scenesErrByEp: Record<number, string>; fetchScenes: (n: number, silent?: boolean) => void;
  redoScene: (ep: number, sc: number) => void; redoBusy: string | null;
  preflightDone: number[]; preflightRunning: number | null;
}) {
  const { t } = useApp();
  return (
    <>
            {/* 剧集列表 */}
            <div className="bg-surface rounded-lg border border-line">
              <div className="p-4 border-b border-line">
                <div className="flex items-center justify-between">
                  <h4 className="font-semibold text-ink-1">{t('wb.episodeList')}</h4>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => (splitPlanOpen ? setSplitPlanOpen(false) : fetchSplitPlan())}
                      title={t('wb.splitPlanHint')}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${FOCUS_RING} ${
                        splitPlanOpen
                          ? 'bg-brand text-white'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1 border border-line'
                      }`}
                    >
                      {splitPlanOpen ? t('wb.splitPlanCollapse') : t('wb.splitPlan')}
                    </button>
                    <button
                      onClick={() => setPreflightOpen(v => !v)}
                      title={t('wb.preflightHint')}
                      className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${FOCUS_RING} ${
                        preflightOpen
                          ? 'bg-success text-white'
                          : 'bg-surface-2 text-ink-2 hover:bg-line hover:text-ink-1 border border-line'
                      }`}
                    >
                      {t('wb.preflight')}
                    </button>
                  </div>
                </div>
                <p className="text-xs text-ink-2 mt-1">{t('wb.clickEpisodeHint')}</p>
              </div>

              {/* 分集断点提议（已抽到 @/components/SplitPlanCard，E-5） */}
              <SplitPlanCard open={splitPlanOpen} loading={splitPlanLoading} error={splitPlanError} plan={splitPlan} onRetry={fetchSplitPlan} />

              {/* P2-15：补列表语义 —— 此前是一串裸 div/button，读屏不会播报
                  「列表，共 N 项」。这里刻意**不用** <table>：它是可点击的导航列表，
                  不是行列数据，套表格语义反而会误导读屏。 */}
              <ul className="divide-y divide-line">
                {episodes.map((ep: any) => (
                  <li key={ep.episode_no}>
                    <button
                      onClick={() => loadEpisodeDetail(ep.episode_no)}
                      className={`w-full p-4 hover:bg-surface-2 transition-colors text-left ${FOCUS_RING}`}
                    >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          {/* 场次层级（按场次生成）：展开/收起「第1场/第2场/…」；行本身仍点开单集详情 */}
                          <button
                            onClick={(e) => { e.stopPropagation(); toggleScenes(ep.episode_no); }}
                            aria-expanded={scenesOpenEps.includes(ep.episode_no)}
                            title={t('wb.scenesToggle')}
                            className={`shrink-0 rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-1 ${FOCUS_RING}`}
                          >
                            <svg className={`w-4 h-4 transition-transform ${scenesOpenEps.includes(ep.episode_no) ? 'rotate-90' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                            </svg>
                          </button>
                          <span className="flex items-center justify-center w-8 h-8 rounded-full bg-brand-subtle text-brand text-sm font-semibold">
                            {ep.episode_no}
                          </span>
                          <div>
                            <div className="flex items-center gap-1.5">
                              <p className="font-medium text-ink-1">
                                {t('wb.episodeNo', { n: ep.episode_no })}
                                {ep.chapter_title && <span className="ml-2 text-sm text-brand">《{ep.chapter_title}》</span>}
                              </p>
                              {/* 前置解析状态：绿点=已完成（点击查看） / 灰点=可运行 / 转圈=运行中 */}
                              {preflightDone.includes(ep.episode_no) && (
                                <button
                                  onClick={e => { e.stopPropagation(); openPreflightDetail(ep.episode_no); }}
                                  title={t('wb.preflightView')}
                                  className="h-2.5 w-2.5 rounded-full bg-success border-0 p-0 cursor-pointer"
                                />
                              )}
                              {preflightRunning === ep.episode_no && (
                                <div className="w-3 h-3 border-2 border-brand border-t-transparent rounded-full animate-spin" />
                              )}
                              {!preflightDone.includes(ep.episode_no) && preflightRunning !== ep.episode_no && (
                                <button
                                  onClick={e => { e.stopPropagation(); runPreflight(ep.episode_no); }}
                                  title={t('wb.preflightRun')}
                                  className="h-2.5 w-2.5 rounded-full bg-line border border-ink-3 cursor-pointer hover:bg-brand transition-colors"
                                />
                              )}
                            </div>
                            <p className="text-xs text-ink-2 mt-0.5">
                              {t('wb.chapterNo', { n: ep.chapter_index ?? ep.episode_no })}
                            </p>
                          </div>
                        </div>

                        <div className="flex items-center gap-4">
                          <div className="text-right">
                            <div className="text-sm text-ink-2">
                              {t('wb.shotsRatio', { done: ep.completed_shots, total: ep.shot_count })}
                            </div>
                            {ep.created_at && (
                              <div className="text-xs text-ink-3">{ep.created_at.split('T')[0]}</div>
                            )}
                          </div>

                          <span className={`px-2 py-1 rounded-full text-xs font-medium inline-flex items-center gap-1 ${
                            ep.status === 'done' ? 'bg-success-subtle text-success-strong' :
                            ep.status === 'producing' ? 'bg-info-subtle text-info-strong' :
                            ep.status === 'failed' ? 'bg-danger-subtle text-danger-strong' :
                            'bg-surface-2 text-ink-1'
                          }`}>
                            {ep.status === 'done' ? (<><Check className="h-3.5 w-3.5" /> {t('wb.done')}</>) :
                             ep.status === 'producing' ? t('wb.producing') :
                             ep.status === 'failed' ? (<><X className="h-3.5 w-3.5" /> {t('episodes.failed')}</>) :
                             t('ep.pending')}
                          </span>

                          {/* 按场次生成：该集全部场次视频已拼接成整集成片（epNN_full.mp4） */}
                          {scenesByEp[ep.episode_no]?.full_video_ready && (
                            <span className="inline-flex items-center gap-1 rounded-full bg-success-subtle px-2 py-1 text-xs font-medium text-success-strong">
                              <Check className="h-3.5 w-3.5" /> {t('wb.fullVideoReady')}
                            </span>
                          )}

                          <svg className="w-5 h-5 text-ink-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                          </svg>
                        </div>
                      </div>

                      {ep.shot_count > 0 && (
                        <div className="mt-3 w-full bg-line rounded-full h-1.5">
                          <div
                            className={`h-1.5 rounded-full transition-all ${
                              ep.status === 'done' ? 'bg-success' :
                              ep.status === 'failed' ? 'bg-danger' :
                              'bg-brand'
                            }`}
                            style={{ width: `${(ep.completed_shots / ep.shot_count) * 100}%` }}
                          ></div>
                        </div>
                      )}
                    </button>

                    {/* 场次子列表（按场次生成）：第1场/第2场/… + 分镜/视频状态 + 重做本场。
                        既有「点集名展开逐镜详情」不受影响 —— 本面板是新增的场次层级。 */}
                    {scenesOpenEps.includes(ep.episode_no) && (
                      <div className="border-t border-line bg-surface-2/50 px-4 py-3">
                        {scenesLoadingEp === ep.episode_no && !scenesByEp[ep.episode_no] ? (
                          <div className="flex items-center gap-2 py-1 text-sm text-ink-2">
                            <div className="h-4 w-4 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                            {t('wb.scenesLoading')}
                          </div>
                        ) : scenesErrByEp[ep.episode_no] ? (
                          <div className="flex items-center justify-between gap-3 py-1">
                            <span className="text-sm text-danger-strong">{scenesErrByEp[ep.episode_no]}</span>
                            <button
                              onClick={() => fetchScenes(ep.episode_no)}
                              className={`rounded-md bg-surface-2 px-2.5 py-1 text-xs text-ink-2 transition-colors hover:bg-line ${FOCUS_RING}`}
                            >
                              {t('wb.scenesRetry')}
                            </button>
                          </div>
                        ) : (scenesByEp[ep.episode_no]?.scenes?.length ?? 0) === 0 ? (
                          <p className="py-1 text-sm text-ink-3">{t('wb.scenesEmpty')}</p>
                        ) : (
                          <ul className="divide-y divide-line">
                            {(scenesByEp[ep.episode_no]?.scenes || []).map((sc: any) => {
                              const redoKey = `${ep.episode_no}:${sc.scene_no}`;
                              return (
                                <li key={sc.scene_no} className="flex flex-wrap items-center gap-2 py-2.5">
                                  <span className="shrink-0 text-sm font-medium text-ink-1">
                                    {t('wb.sceneNo', { n: sc.scene_no })}
                                  </span>
                                  {sc.heading && (
                                    <span className="max-w-[18rem] truncate text-xs text-ink-2" title={sc.heading}>{sc.heading}</span>
                                  )}
                                  {sc.location && (
                                    <span className="text-xs text-ink-3">{sc.location}</span>
                                  )}
                                  <span className="tabular-nums text-xs text-ink-2">{t('wb.sceneShotCount', { n: sc.shot_count })}</span>
                                  {/* 分镜状态：齐了亮绿「分镜完成」，否则显示「分镜 k/N」 */}
                                  {sc.shot_count > 0 && sc.storyboard_ok >= sc.shot_count ? (
                                    <span className="rounded bg-success-subtle px-1.5 py-0.5 text-xs font-medium text-success-strong">
                                      {t('wb.sceneStoryboardDone')}
                                    </span>
                                  ) : (
                                    <span className="rounded bg-warning-subtle px-1.5 py-0.5 text-xs font-medium text-warning-strong">
                                      {t('wb.sceneStoryboardPart', { done: sc.storyboard_ok, total: sc.shot_count })}
                                    </span>
                                  )}
                                  {/* 视频状态：该场 scene_XX.mp4 是否已生成 */}
                                  {sc.video_ready ? (
                                    <span className="rounded bg-success-subtle px-1.5 py-0.5 text-xs font-medium text-success-strong">
                                      ✓ {t('wb.sceneVideoReady')}
                                    </span>
                                  ) : (
                                    <span className="rounded bg-surface-2 px-1.5 py-0.5 text-xs font-medium text-ink-3">
                                      {t('wb.sceneVideoMissing')}
                                    </span>
                                  )}
                                  <button
                                    onClick={() => redoScene(ep.episode_no, sc.scene_no)}
                                    disabled={redoBusy !== null}
                                    title={t('wb.sceneRedoHint')}
                                    className={`ml-auto inline-flex shrink-0 items-center gap-1 rounded-md border border-line bg-surface px-2 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-line hover:text-ink-1 disabled:opacity-60 ${FOCUS_RING}`}
                                  >
                                    {redoBusy === redoKey && (
                                      <span className="h-3 w-3 animate-spin rounded-full border-2 border-brand border-t-transparent" />
                                    )}
                                    {redoBusy === redoKey ? t('wb.sceneRedoBusy') : t('wb.sceneRedo')}
                                  </button>
                                </li>
                              );
                            })}
                          </ul>
                        )}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </div>
    </>
  );
}
