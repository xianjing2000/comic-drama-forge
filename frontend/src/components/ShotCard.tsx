/** 分镜卡（E-5 2026-10-16 从 StoryboardTab 抽出的**展示型**组件；状态/回调由父组件经 props 注入）。 */
import { useApp } from '@/context/AppContext';
import { Button, ReorderControls } from '@/components/ui';
import { FOCUS_RING } from '@/components/ui/focusRing';
import { shotQuality, criticalIssuesOf, softReasonOf } from '@/utils/storyboardQuality';
import { BATCH_RETRY_LIMIT } from '@/pages/workbenchShared';

export function ShotCard({ card, cardIndex, total, busy, videoMode, selectedShots,
  dragSid, dragOverSid, reorderBusy, batchRunning, focusHighlight,
  playingVideo, setPlayingVideo, setVideoMode, toggleShotSelected, dropShotOn, moveShot,
  setDragSid, setDragOverSid, setZoomImg, handleRetryImage, handleRetryVideo }: {
  card: any; cardIndex: number; total: number;
  busy: string | null; videoMode: Record<string, 'reference' | 'keyframe'>; selectedShots: string[];
  dragSid: string | null; dragOverSid: string | null; reorderBusy: boolean; batchRunning: boolean;
  focusHighlight: string | null;
  playingVideo: string | null; setPlayingVideo: (v: any) => void; setVideoMode: (v: any) => void;
  toggleShotSelected: (sid: string) => void; dropShotOn: (sid: string) => void;
  moveShot: (sid: string, dir: -1 | 1) => void; setDragSid: (v: any) => void; setDragOverSid: (v: any) => void;
  setZoomImg: (v: { src: string; seq: number; grid?: boolean } | null) => void;
  handleRetryImage: (card: any) => void; handleRetryVideo: (card: any) => void;
}) {
  const { t } = useApp();
          const sid = String(card.shot_id);
          const imgBusy = busy === `${card.shot_id}:image`;
          const vidBusy = busy === `${card.shot_id}:video`;
          const mode = videoMode[sid] || 'reference';
          const isSelected = selectedShots.includes(sid);
          return (
            <div
              key={card.seq}
              // t9：音频体检「定位分镜」按此属性在本容器内查找目标卡（不依赖 DOM 顺序）
              data-shot-id={sid}
              // 拖拽落点：只有「有拖拽在途」时才接管，避免把其它点击/选择行为搅乱
              onDragOver={(e) => {
                if (dragSid && dragSid !== sid && !reorderBusy) {
                  e.preventDefault();
                  setDragOverSid(sid);
                }
              }}
              onDrop={(e) => {
                if (!dragSid) return;
                e.preventDefault();
                dropShotOn(sid);
              }}
              onDragLeave={() => setDragOverSid((prev: any) => (prev === sid ? null : prev))}
              className={`bg-surface rounded-lg border p-4 flex flex-col transition-colors ${
                dragOverSid === sid && dragSid && dragSid !== sid ? 'border-brand bg-surface-2' : 'border-line'
              } ${dragSid === sid ? 'opacity-60' : ''} ${
                focusHighlight === sid ? 'ring-2 ring-brand ring-offset-2 ring-offset-canvas' : ''
              }`}
            >
              <div className="flex items-center justify-between mb-2">
                <label className="flex items-center gap-1.5 cursor-pointer">
                  {/* 批量重生成勾选：已达上限时未勾选的复选框禁用（原生 checkbox，与 QcTab 一致） */}
                  <input
                    type="checkbox"
                    checked={isSelected}
                    disabled={batchRunning || (!isSelected && selectedShots.length >= BATCH_RETRY_LIMIT)}
                    onChange={() => toggleShotSelected(sid)}
                    aria-label={t('video.batchRetry.pickShot', { seq: card.seq })}
                    className={`rounded ${FOCUS_RING}`}
                  />
                  <span className="font-mono text-sm text-ink-2">#{card.seq}</span>
                </label>
                <span className="flex min-w-0 items-center gap-2">
                  {/* ⭐ t12：质量徽标 —— 软放行 / 严重问题必须一眼可见（卡片不再"全绿"） */}
                  {(() => {
                    const q = shotQuality(card.storyboard);
                    if (q === 'soft') {
                      const ci = criticalIssuesOf(card.storyboard);
                      return (
                        <span
                          className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] leading-4 border ${
                            ci.length
                              ? 'bg-danger-subtle text-danger-strong border-danger/40'
                              : 'bg-warning-subtle text-warning-strong border-warning/40'
                          }`}
                          title={ci.length ? ci.join('；') : softReasonOf(card.storyboard)}
                        >
                          {ci.length
                            ? t('sb.quality.criticalBadge', { n: ci.length })
                            : t('sb.quality.softBadge')}
                        </span>
                      );
                    }
                    if (q === 'blocked') {
                      return (
                        <span className="shrink-0 rounded border border-danger/40 bg-danger-subtle px-1.5 py-0.5 text-[11px] leading-4 text-danger-strong">
                          {t('sb.quality.blockedBadge')}
                        </span>
                      );
                    }
                    return null;
                  })()}
                  <span className="truncate text-xs text-ink-2">{card.camera}</span>
                  {/* 排序控件：手柄拖拽 + ↑/↓ 精确挪一格（文案在组件内走 i18n，不硬编码） */}
                  <ReorderControls
                    index={cardIndex}
                    total={total}
                    busy={reorderBusy || batchRunning}
                    dragActive={dragSid === sid}
                    onMoveUp={() => moveShot(sid, -1)}
                    onMoveDown={() => moveShot(sid, 1)}
                    onDragStart={(e) => {
                      // Firefox 不设 dataTransfer 就不触发拖拽事件链
                      e.dataTransfer.effectAllowed = 'move';
                      e.dataTransfer.setData('text/plain', sid);
                      setDragSid(sid);
                    }}
                    onDragEnd={() => { setDragSid(null); setDragOverSid(null); }}
                  />
                </span>
              </div>

              <div className="flex gap-3 mb-2">
                {(() => {
                  // 有可放大查看的图（正式图优先，其次生成中 scratch）才让缩略框可点击
                  const zoomSrc = (card.storyboard?.exists && card.storyboard?.url)
                    || (card.storyboard?.generating && card.storyboard?.scratch_url);
                  return (
                <div
                  role={zoomSrc ? 'button' : undefined}
                  tabIndex={zoomSrc ? 0 : undefined}
                  aria-label={zoomSrc ? t('sb.zoomTitle') : undefined}
                  onClick={zoomSrc ? () => setZoomImg({ src: zoomSrc, seq: card.seq, grid: !!card.storyboard?.grid }) : undefined}
                  onKeyDown={(e) => {
                    if (zoomSrc && (e.key === 'Enter' || e.key === ' ')) {
                      e.preventDefault();
                      setZoomImg({ src: zoomSrc, seq: card.seq, grid: !!card.storyboard?.grid });
                    }
                  }}
                  className={`w-24 h-24 shrink-0 rounded bg-surface-2 border overflow-hidden flex items-center justify-center text-xs text-ink-3 ${
                    zoomSrc ? `cursor-zoom-in ${FOCUS_RING}` : ''
                  } ${
                    card.storyboard?.generating && !card.storyboard?.exists
                      ? 'border-brand/50'
                      : 'border-line'
                  }`}
                >
                  {card.storyboard?.exists && card.storyboard?.url ? (
                    <img src={card.storyboard.url} alt={t('sb.imageAlt', { seq: card.seq })} className="w-full h-full object-cover" />
                  ) : card.storyboard?.generating && card.storyboard?.scratch_url ? (
                    // 生成中：正式产物尚未落盘，但已有中间产物 → 显示实时缩略图 + 角标
                    // ⚠️ 中间产物可能被后续 try 覆盖 → 加时间戳查询参数绕过浏览器缓存
                    <div className="relative w-full h-full" title={t('sb.generatingHint')}>
                      <img
                        src={`${card.storyboard.scratch_url}${card.storyboard.scratch_url.includes('?') ? '&' : '?'}t=${Date.now()}`}
                        alt={t('sb.imageAlt', { seq: card.seq })}
                        className="w-full h-full object-cover"
                      />
                      <span className="absolute inset-x-0 bottom-0 bg-brand/85 text-white text-xs leading-4 text-center">
                        {t('sb.generating')}
                      </span>
                    </div>
                  ) : card.storyboard?.generating ? (
                    <span className="flex flex-col items-center gap-1 text-brand">
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-brand" />
                      <span>{t('sb.generating')}</span>
                    </span>
                  ) : (
                    <span>{t('sb.noImage')}</span>
                  )}
                </div>
                  );
                })()}
                <div className="min-w-0 flex-1 text-xs space-y-1">
                  <p className="text-ink-1 line-clamp-3">{card.description}</p>
                  {card.dialogue_text && (
                    <p className="text-ink-2 italic line-clamp-2">{card.dialogue_text}</p>
                  )}
                  {/* ⭐ t12：软放行必须就地说明「为什么放行 / 有什么严重问题」——
                      否则用户仍会把这镜当成合格产物。 */}
                  {shotQuality(card.storyboard) === 'soft' && (
                    <>
                      <p className="text-warning-strong line-clamp-2">
                        {t('sb.quality.softLine', {
                          reason: softReasonOf(card.storyboard) || t('sb.quality.softNoReason'),
                        })}
                      </p>
                      {criticalIssuesOf(card.storyboard).length > 0 && (
                        <p className="text-danger-strong line-clamp-2">
                          {t('sb.quality.criticalLine', {
                            text: criticalIssuesOf(card.storyboard)[0],
                          })}
                        </p>
                      )}
                    </>
                  )}
                  {/* ⭐ 2026-10-17：质检阻断同样要就地写清「问题在哪」——
                      旧实现只有一枚「质检阻断」徽标、不写原因，用户无从知道为什么没过。 */}
                  {shotQuality(card.storyboard) === 'blocked' && (
                    <>
                      <p className="text-danger-strong line-clamp-2">
                        {t('sb.quality.blockedLine', {
                          reason: softReasonOf(card.storyboard) || t('sb.quality.blockedNoReason'),
                        })}
                      </p>
                      {criticalIssuesOf(card.storyboard).length > 0 && (
                        <p className="text-danger-strong line-clamp-2">
                          {t('sb.quality.criticalLine', {
                            text: criticalIssuesOf(card.storyboard)[0],
                          })}
                        </p>
                      )}
                    </>
                  )}
                  <p className={card.video?.exists ? 'text-success-strong' : 'text-warning-strong'}>
                    {t('sb.video')}：{card.video?.exists ? t('sb.generated') : t('sb.notGenerated')}
                  </p>
                  {card.consistency?.score != null && (
                    <p className="text-ink-2">{t('sb.consistency')}：{card.consistency.score}</p>
                  )}
                </div>
              </div>

              {/* 内嵌播放器：插在信息区之后、操作区（带 mt-auto）之前，
                  使按钮仍被压到卡底，且与卡片内 mb-2 的间距风格一致。
                  ⚠️ 不写死高度、不加 aspect-video —— 重跑/超分后分辨率会变，交给浏览器按元数据自适应。 */}
              {playingVideo === sid && card.video?.url && (
                <video src={card.video.url} controls className="w-full mt-2 rounded-lg bg-black" />
              )}

              <div className="flex flex-wrap items-center gap-2 mt-auto pt-2">
                {card.video?.exists && card.video?.url && (
                  <>
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => setPlayingVideo(playingVideo === sid ? null : sid)}
                    >
                      {playingVideo === sid ? t('common.close') : t('common.play')}
                    </Button>
                    {/* 端点未设 as_attachment，只能靠 HTML download 属性触发下载（同源，有效） */}
                    <a
                      href={card.video.url}
                      download
                      className={`text-xs text-brand hover:text-brand rounded-sm ${FOCUS_RING}`}
                    >
                      {t('common.download')}
                    </a>
                  </>
                )}
                <select
                  value={mode}
                  onChange={(e) =>
                    setVideoMode((prev: any) => ({ ...prev, [sid]: e.target.value as 'reference' | 'keyframe' }))
                  }
                  className={`text-xs rounded border border-line bg-surface text-ink-1 px-1 py-1 ${FOCUS_RING}`}
                  title={t('sb.modeHint')}
                >
                  <option value="reference">{t('sb.modeReference')}</option>
                  <option value="keyframe">{t('sb.modeKeyframe')}</option>
                </select>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() => handleRetryImage(card)}
                  disabled={!!busy}
                >
                  {imgBusy ? t('sb.redoing') : t('sb.redoImage')}
                </Button>
                <Button
                  size="sm"
                  onClick={() => handleRetryVideo(card)}
                  disabled={!!busy}
                >
                  {vidBusy ? t('sb.redoing') : t('sb.redoVideo')}
                </Button>
              </div>
            </div>
          );
}
