// ============================================
// 单集审片面板（四层质量状态 · 逐镜并排复核）
// ============================================
// 2026-09-29：从原独立「审片」标签页抽出 —— 现在它嵌在「审片与验收」里
// **每一集的成片验收行**内展开，不再是一个独立标签页、也不是独立区块。
//
// 数据口径（与后端 /api/quality/* 字段一一对应）：
//   单集载荷 → GET /api/quality/review（契约 / 分镜 / 参考 / 视频 / 质检 一次取全）
//   人工判定 → POST /api/quality/stage（C / D；通过会绑定产物哈希）
//   预演批准 → POST /api/videos/preview/approve（两级生产的第二阶段）
//
// A / B 层（技术生成 / 内容质检）由系统判定，这里只读展示 —— 后端接口也不接受人工写 A / B。
import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import {
  qualityApi,
  type QualityReviewResponse,
  type QualityStageInfo,
  type ReviewShot,
} from '@/api/client';
import { Button, Textarea, Skeleton, ErrorState, Badge } from '@/components/ui';
import { Eye, Film, ChevronRight, ChevronDown } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';

// ---- helpers ----------------------------------------------------------------

const cx = (...parts: Array<string | false | null | undefined>): string =>
  parts.filter(Boolean).join(' ');

type TFunc = (key: string, params?: Record<string, string | number>) => string;
type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'info';

/** 四层状态的圆点配色（成片验收行里的 A/B/C/D 小圆点也用它） */
export const STATUS_DOT: Record<string, string> = {
  passed: 'bg-success',
  failed: 'bg-danger',
  blocked: 'bg-danger',
  pending: 'bg-ink-2/40',
  skipped: 'bg-ink-2/25',
};

function statusKey(status: string): string {
  if (status === 'passed') return 'statusPassed';
  if (status === 'failed') return 'statusFailed';
  if (status === 'blocked') return 'statusBlocked';
  if (status === 'skipped') return 'statusSkipped';
  return 'statusPending';
}

// ---- StageChip（A/B 系统只读；C/D 人工） ------------------------------------

function StageChip({ stage, label, info, human, t }: {
  stage: string;
  label: string;
  info?: QualityStageInfo;
  human: boolean;
  t: TFunc;
}) {
  const st = info?.status || 'pending';
  return (
    <div
      className="flex items-center gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2"
      title={human ? t('review.layerHuman') : t('review.layerCpu')}
    >
      <span className={cx('h-2.5 w-2.5 rounded-full', STATUS_DOT[st])} />
      <span className="text-xs font-semibold text-ink-3">{stage}</span>
      <span className="text-xs text-ink-1">{label}</span>
      <span className={cx(
        'text-xs font-medium',
        st === 'passed'
          ? 'text-success-strong'
          : st === 'failed' || st === 'blocked'
            ? 'text-danger-strong'
            : 'text-ink-2'
      )}>
        {t('review.' + statusKey(st))}
      </span>
      {info?.has_binding && (
        <span className="text-xs text-ink-3" title={t('review.bindNote')}>🔒</span>
      )}
    </div>
  );
}

// ---- ShotCard（逐镜并排：分镜·参考·成果·QC） -------------------------------

function ShotCard({ shot, t }: { shot: ReviewShot; t: TFunc }) {
  const qc = shot.qc;
  const latest = qc.latest || {};
  let qcVariant: BadgeVariant = 'default';
  let qcLabel = t('review.qaNone');
  if (qc.found) {
    if (latest.ok === false) {
      qcVariant = 'danger';
      qcLabel = t('review.qaFault');
    } else if (latest.passed || qc.last_passed) {
      qcVariant = 'success';
      qcLabel = t('review.qaPass');
    } else {
      qcVariant = 'danger';
      qcLabel = t('review.qaFail');
    }
  }

  return (
    <div className="rounded-lg border border-line bg-surface p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-semibold text-ink-1">
            {t('review.shotCard', { n: shot.seq })}
          </span>
          {shot.duration != null && (
            <span className="text-xs text-ink-2">{shot.duration}s</span>
          )}
          {shot.camera && <span className="text-xs text-ink-3">{shot.camera}</span>}
          {shot.location && (
            <span className="text-xs text-ink-2/70 truncate max-w-[120px]">{shot.location}</span>
          )}
        </div>
        <Badge variant={qcVariant}>{qcLabel}</Badge>
      </div>

      {/* Three columns: storyboard | refs | result */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {/* Col 1: Storyboard */}
        <div className="rounded border border-line bg-surface-2 overflow-hidden">
          {shot.storyboard.exists ? (
            <img
              src={shot.storyboard.url}
              alt=""
              className="w-full aspect-video object-cover"
              loading="lazy"
            />
          ) : (
            <div className="flex items-center justify-center aspect-video text-xs text-ink-3">
              {t('review.noStoryboard')}
            </div>
          )}
          <div className="px-2 py-1 text-xs text-ink-3">{t('review.storyboardSide')}</div>
        </div>

        {/* Col 2: Reference assets */}
        <div className="rounded border border-line bg-surface-2 p-2">
          {shot.refs.length > 0 ? (
            <div className="grid grid-cols-2 gap-1">
              {shot.refs.slice(0, 6).map((r, i) => (
                <div key={i} className="rounded overflow-hidden border border-line/50">
                  <img
                    src={r.url}
                    alt={r.name}
                    className="w-full aspect-square object-cover"
                    loading="lazy"
                  />
                </div>
              ))}
            </div>
          ) : (
            <div className="flex items-center justify-center aspect-video text-xs text-ink-3">
              {t('review.noRefs')}
            </div>
          )}
          <div className="px-2 py-1 text-xs text-ink-3">{t('review.refSide')}</div>
        </div>

        {/* Col 3: Result */}
        <div className="rounded border border-line bg-surface-2 overflow-hidden">
          {shot.video.exists ? (
            <video
              src={shot.video.url}
              controls
              preload="metadata"
              className="w-full aspect-video"
            />
          ) : (
            <div className="flex items-center justify-center aspect-video text-xs text-ink-3">
              {t('review.noVideo')}
            </div>
          )}
          <div className="px-2 py-1 text-xs text-ink-3">{t('review.resultSide')}</div>
        </div>
      </div>

      {/* Contract text */}
      <div className="space-y-1 text-sm">
        {shot.description && <p className="text-ink-1">{shot.description}</p>}
        {shot.dialogue_text && (
          <p className="text-ink-1 italic">「{shot.dialogue_text}」</p>
        )}
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-ink-2">
          {shot.first_frame && <span>{t('review.firstFrame')}: {shot.first_frame}</span>}
          {shot.last_frame && <span>{t('review.lastFrame')}: {shot.last_frame}</span>}
          {shot.motion && <span>{t('review.motion')}: {shot.motion}</span>}
          {shot.emotion && <span>emotion: {shot.emotion}</span>}
          {shot.beat && <span>beat: {shot.beat}</span>}
        </div>
      </div>

      {/* QC detail */}
      {qc.found && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
          {latest.score != null && (
            <span className="font-medium">{t('review.score')}: {latest.score}</span>
          )}
          {qc.attempts > 0 && (
            <span>{t('review.qaAttempts', { n: qc.attempts })}</span>
          )}
          {latest.reason && (
            <span className="max-w-[300px] truncate">{latest.reason}</span>
          )}
          {(latest.issues || []).slice(0, 2).map((iss: string, i: number) => (
            <span key={i} className="text-warning-strong">· {iss}</span>
          ))}
        </div>
      )}
    </div>
  );
}

// ---- StageCard（C/D 操作卡：通过 / 打回 + 备注 + 阻塞提示） -----------------

function StageCard({ stage, info, note, setNote, busy, onAct, artifactExists, release, staleReason, t }: {
  stage: 'C' | 'D';
  info?: QualityStageInfo;
  note: string;
  setNote: (v: string) => void;
  busy: string;
  onAct: (stage: 'C' | 'D', status: string) => void;
  artifactExists: boolean;
  release?: { ready: boolean; blockers: string[] };
  staleReason?: string;
  t: TFunc;
}) {
  const isD = stage === 'D';
  const st = info?.status || 'pending';
  const sk = statusKey(st);
  const variant: BadgeVariant =
    st === 'passed' ? 'success'
      : st === 'failed' || st === 'blocked' ? 'danger'
        : 'default';

  return (
    <div className="rounded-lg border border-line bg-surface p-4 space-y-3">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className={cx('h-2.5 w-2.5 rounded-full', STATUS_DOT[st])} />
          <span className="text-sm font-semibold text-ink-1">
            {isD ? t('review.stageD') : t('review.stageC')}
          </span>
          <Badge variant={variant}>{t('review.' + sk)}</Badge>
          {info?.has_binding && (
            <span className="text-xs text-ink-3" title={t('review.bindNote')}>🔒</span>
          )}
        </div>
        {info?.at && (
          <span className="text-xs text-ink-2">{t('review.actedAt', { t: info.at })}</span>
        )}
      </div>

      {/* Description */}
      <p className="text-xs text-ink-2">
        {isD ? t('review.stageDDesc') : t('review.stageCDesc')} · {t('review.bindNote')}
      </p>

      {/* Previous note */}
      {info?.note && (
        <p className="text-xs text-ink-1 bg-surface-2 rounded px-2 py-1">{info.note}</p>
      )}

      {/* Stale reason */}
      {staleReason && (
        <div className="rounded border border-danger/30 bg-danger-subtle px-3 py-2 text-xs text-danger-strong">
          ⚠ {t('review.staleTitle')} · {staleReason} · {t('review.staleHint')}
        </div>
      )}

      {/* D-specific: release blockers */}
      {isD && release && release.blockers.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-ink-2">{t('review.blockersTitle')}</p>
          {release.blockers.map((b, i) => (
            <p key={i} className="text-xs text-ink-2">· {b}</p>
          ))}
        </div>
      )}

      {/* Note input */}
      <Textarea
        value={note}
        onChange={setNote}
        rows={2}
        placeholder={t('review.notePh')}
      />

      {/* Actions */}
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="brand"
          disabled={!artifactExists || busy !== ''}
          loading={busy === stage}
          onClick={() => onAct(stage, 'passed')}
        >
          {isD ? t('review.approveRelease') : t('review.approve')}
        </Button>
        <Button
          size="sm"
          variant="danger"
          disabled={busy !== ''}
          onClick={() => onAct(stage, 'failed')}
        >
          {t('review.reject')}
        </Button>
      </div>

      {/* Missing artifact hint */}
      {!artifactExists && (
        <p className="text-xs text-warning-strong">⚠ {t('review.artifactMissing')}</p>
      )}
    </div>
  );
}

// ---- 主组件 -----------------------------------------------------------------

export function EpisodeReviewPanel({ projectKey, episode, onChanged }: {
  projectKey: string;
  /** 要复核的集号（成片验收行里的那一集） */
  episode: number;
  /** C/D 判定或预演批准成功后的回调：让外层刷新列表与验收状态 */
  onChanged?: () => void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [data, setData] = useState<QualityReviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');

  // ⭐ 2026-10-09：场次组折叠。一集 41 镜 × 三列并排会拉出一屏多的长度 →
  //   默认**只展开第一个场次**，其余收起，点标题行切换。
  //   sceneToggled 只记录**用户显式切换过**的场次；没记录过的按默认走，
  //   这样「默认仅首个展开」与「用户点开任意场次」两套语义不打架。
  const [sceneToggled, setSceneToggled] = useState<Record<string, boolean>>({});
  const isSceneCollapsed = (nm: string, isFirst: boolean) =>
    sceneToggled[nm] !== undefined ? sceneToggled[nm] : !isFirst;
  const toggleScene = (nm: string, isFirst: boolean) =>
    setSceneToggled((p) => ({
      ...p,
      [nm]: !(p[nm] !== undefined ? p[nm] : !isFirst),
    }));
  const [note, setNote] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setData(await qualityApi.review(projectKey, episode));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(t('common.loadFailed')));
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [projectKey, episode, t]);

  useEffect(() => { void load(); }, [load]);

  const act = async (stage: 'C' | 'D', status: string) => {
    if (busy) return;
    setBusy(stage);
    setError('');
    try {
      await qualityApi.setStage({ project: projectKey, episode, stage, status, note });
      toast.success(String(t('review.approvedOk')));
      setNote('');
      await load();
      onChanged?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(t('wb.actionFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setBusy('');
    }
  };

  const approvePreview = async () => {
    if (busy) return;
    setBusy('preview');
    setError('');
    try {
      await qualityApi.approvePreview({ project: projectKey, episode_no: episode });
      toast.success(String(t('review.approvedOk')));
      await load();
      onChanged?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(t('wb.actionFailed'));
      setError(msg);
      toast.error(msg);
    } finally {
      setBusy('');
    }
  };

  // 展开即加载：骨架与真实布局一致，避免展开时高度跳变
  if (loading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-6 w-48" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-80 w-full" />
      </div>
    );
  }

  if (!data) {
    return (
      <ErrorState
        title={String(t('common.loadFailed'))}
        description={error}
        onRetry={load}
      />
    );
  }

  return (
    <div className="space-y-4">
      {/* Soft error banner */}
      {error && (
        <div className="rounded border border-danger/30 bg-danger-subtle px-3 py-2 text-sm text-danger-strong">
          {error}
        </div>
      )}

      {/* Title + contract summary */}
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-semibold text-ink-1">
            {t('review.epN', { n: data.episode })} {data.contract?.title}
          </h4>
          <p className="text-xs text-ink-2">
            {t('review.shotCount', { n: data.contract?.shots_total || 0 })}
            {' · '}
            {data.contract?.duration_sec != null
              ? Math.round(data.contract.duration_sec) + 's'
              : ''}
          </p>
        </div>
        {data.release?.ready && (
          <Badge variant="success">{t('review.releaseReady')}</Badge>
        )}
      </div>

      {/* Stage chips */}
      <div className="flex flex-wrap gap-2">
        {(['A', 'B'] as const).map((s) => (
          <StageChip
            key={s}
            stage={s}
            label={data.state[s]?.label || s}
            info={data.state[s]}
            human={false}
            t={t}
          />
        ))}
        {(['C', 'D'] as const).map((s) => (
          <StageChip
            key={s}
            stage={s}
            label={data.state[s]?.label || s}
            info={data.state[s]}
            human={true}
            t={t}
          />
        ))}
      </div>

      {/* Stale banner */}
      {(data.stale?.C || data.stale?.D) && (
        <div className="rounded-lg border border-danger/30 bg-danger-subtle px-4 py-3 space-y-1">
          <p className="text-sm font-medium text-danger-strong">
            ⚠ {t('review.staleTitle')}
          </p>
          {data.stale.C && (
            <p className="text-xs text-danger-strong">
              {t('review.stalePrefix', { s: 'C' })}: {data.stale.C}
            </p>
          )}
          {data.stale.D && (
            <p className="text-xs text-danger-strong">
              {t('review.stalePrefix', { s: 'D' })}: {data.stale.D}
            </p>
          )}
          <p className="text-xs text-ink-2">{t('review.staleHint')}</p>
        </div>
      )}

      {/* Preview banner */}
      {data.preview?.exists && (
        <div className="rounded-lg border border-warning/30 bg-warning-subtle p-4 space-y-3">
          <div className="flex items-center gap-2">
            <Eye className="h-4 w-4 text-warning-strong" />
            <span className="text-sm font-medium text-warning-strong">
              {t('review.previewBanner')}
            </span>
          </div>
          <Button
            size="sm"
            variant="brand"
            disabled={busy !== ''}
            loading={busy === 'preview'}
            onClick={approvePreview}
          >
            {t('review.approvePreview')}
          </Button>
        </div>
      )}

      {/* Artifact player */}
      {data.artifact?.exists ? (
        <div className="rounded-lg overflow-hidden border border-line">
          <video
            src={data.artifact.url}
            controls
            preload="metadata"
            className="w-full"
          />
        </div>
      ) : (
        <div className="rounded-lg border border-line bg-surface-2 px-4 py-3 text-sm text-ink-2 flex items-center gap-2">
          <Film className="h-4 w-4" />
          <span>{t('review.artifactMissing')}</span>
        </div>
      )}

      {/* 场次分组 + 镜头卡片
          ⭐ 2026-10-09：由「41 镜平铺」改为**按场次分组**（用户要求：顶层按整集、点开按场次）。
          分组键取 shots[].refs 里 kind==="scenes" 的 name —— ⚠️ 是复数 **scenes**，
          不是 scene（写错就永远匹配不上，退回 location 兜底）。
          实测第1集 41 镜 → 8 个场次，镜数 10/1/7/7/7/1/3/5 合计 41，
          与剧本顶层 scenes[] 的 8 个场次一一对应。
          场次顺序＝该场次首镜的出现顺序（镜头本就是顺序的，等价于剧本场次顺序）。 */}
      {data.shots.length > 0 && (
        <div className="space-y-5">
          {(() => {
            const groups: Array<{ name: string; shots: typeof data.shots }> = [];
            const idx: Record<string, number> = {};
            data.shots.forEach((shot) => {
              const sref = (shot.refs || []).find(
                (r: { kind?: string; name?: string }) => r.kind === 'scenes' || r.kind === 'scene',
              );
              const nm = String(sref?.name || shot.location || '未命名场次');
              if (!(nm in idx)) { idx[nm] = groups.length; groups.push({ name: nm, shots: [] }); }
              groups[idx[nm]].shots.push(shot);
            });
            return groups.map((g, gi) => (
              <div key={g.name}>
                <button
                  type="button"
                  onClick={() => toggleScene(g.name, gi === 0)}
                  className="mb-2 flex w-full flex-wrap items-center gap-2 rounded-md border border-line bg-surface-2 px-3 py-2 text-left transition-colors hover:bg-surface-2/70"
                >
                  {isSceneCollapsed(g.name, gi === 0)
                    ? <ChevronRight className="h-3.5 w-3.5 text-ink-3" />
                    : <ChevronDown className="h-3.5 w-3.5 text-ink-3" />}
                  <span className="text-sm font-medium text-ink-1">{g.name}</span>
                  <Badge variant="default">{g.shots.length} 镜</Badge>
                  <span className="text-xs text-ink-3">
                    {`镜 ${g.shots[0]?.seq}–${g.shots[g.shots.length - 1]?.seq}`}
                  </span>
                  <span className="ml-auto text-xs text-ink-3">
                    {isSceneCollapsed(g.name, gi === 0) ? '展开' : '收起'}
                  </span>
                </button>
                {!isSceneCollapsed(g.name, gi === 0) && (
                  <div className="space-y-3">
                    {g.shots.map((shot) => <ShotCard key={shot.seq} shot={shot} t={t} />)}
                  </div>
                )}
              </div>
            ));
          })()}
        </div>
      )}

      {/* C / D action cards */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <StageCard
          stage="C"
          info={data.state.C}
          note={note}
          setNote={setNote}
          busy={busy}
          onAct={act}
          artifactExists={!!data.artifact?.exists}
          staleReason={data.stale?.C}
          t={t}
        />
        <StageCard
          stage="D"
          info={data.state.D}
          note={note}
          setNote={setNote}
          busy={busy}
          onAct={act}
          artifactExists={!!data.artifact?.exists}
          release={data.release}
          staleReason={data.stale?.D}
          t={t}
        />
      </div>
    </div>
  );
}
