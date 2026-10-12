import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { sanitizeErrorMessage } from '@/utils/errorText';
import { memoryApi, projectsApi } from '@/api/client';
import { Card, Button, Loading, EmptyState, Badge, ConfirmDialog, Input, Select, Skeleton, ErrorState } from '@/components/ui';
import { BookOpen, Brain } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import type { Memory, MemoryType, MemoryStats, PromptLesson, LessonPage, Project } from '@/types';

/**
 * 教训类型 → i18n 键（避免界面出现原始 kind 值）。
 * ⚠️ 与 ProjectWorkbenchPage 里的 KIND_LABEL 是两份独立的映射（那份管「质检品类」，
 * 这份管「教训环节」），7 类标签按设计文档 §7 第 7 条单一事实源对齐：
 *   asset=资产 / storyboard=分镜 / video=视频 / keyframe=尾帧 /
 *   audio=配音 / script=剧本 / prompt=提示词预检
 */
const LESSON_KIND_LABEL: Record<string, string> = {
  asset: 'memory.lessonKind.asset',
  storyboard: 'memory.lessonKind.storyboard',
  video: 'memory.lessonKind.video',
  keyframe: 'memory.lessonKind.keyframe',
  audio: 'memory.lessonKind.audio',
  script: 'memory.lessonKind.script',
  prompt: 'memory.lessonKind.prompt',
};

/** 7 类环节（与后端 kind 枚举单一事实源一致，固定顺序渲染 chips） */
const LESSON_KINDS: string[] = ['asset', 'storyboard', 'video', 'keyframe', 'audio', 'script', 'prompt'];

/** 每页条数（配合后端 offset/limit 分页） */
const PAGE_SIZE = 20;

/** 从 lesson 的 context/category/priority 里拼出「命中来源」的可读文案 */
function lessonSourceLabel(
  l: PromptLesson,
  t: (key: string, params?: Record<string, string | number>) => string,
): string {
  const ctx = l.context && typeof l.context === 'object' ? l.context : {};
  const keys = Object.keys(ctx);
  if (keys.length > 0) {
    return keys.map((k) => `${k}=${String((ctx as Record<string, unknown>)[k])}`).join('，');
  }
  const parts: string[] = [];
  if (l.category) parts.push(t('memory.categoryLabel', { value: l.category }));
  if (l.priority) parts.push(t('memory.priorityLabel', { value: l.priority }));
  return parts.length > 0 ? parts.join('，') : '—';
}

export function MemoryPage() {
  const { t } = useApp();
  const toast = useToast();
  /** 教训环节 kind → 本地化标签；未知 kind 回退为通用「未知」 */
  const lessonKindLabel = (kind?: string): string => {
    const key = LESSON_KIND_LABEL[kind || ''];
    return key ? t(key) : t('common.unknown');
  };
  const [memories, setMemories] = useState<Memory[]>([]);
  const [stats, setStats] = useState<MemoryStats>({
    total: 0, lessons: 0, successes: 0, insights: 0, promptLessons: 0,
  });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [insights, setInsights] = useState<string[]>([]);
  const [insightsLoading, setInsightsLoading] = useState(false);

  // ---- 质检教训库（生成链路自动沉淀；与手动登记的 memories 是两套数据）----
  const [projects, setProjects] = useState<Project[]>([]);
  /** 已选环节（多选）；空数组 = 全部 */
  const [lessonKinds, setLessonKinds] = useState<string[]>([]);
  const [lessonProject, setLessonProject] = useState('');
  const [lessonSince, setLessonSince] = useState('');
  const [lessonUntil, setLessonUntil] = useState('');
  /** 检索框输入（回车后才生效） */
  const [lessonSearch, setLessonSearch] = useState('');
  /** 已生效的关键词（触发重查） */
  const [lessonQ, setLessonQ] = useState('');
  const [lessons, setLessons] = useState<PromptLesson[]>([]);
  const [lessonPage, setLessonPage] = useState<LessonPage>({
    total: 0, filtered: 0, by_kind: {}, dead_lessons: 0, lessons: [],
  });
  const [lessonOffset, setLessonOffset] = useState(0);
  const [lessonLoading, setLessonLoading] = useState(false);
  /**
   * C3（2026-09-23 收口）：教训库加载失败的**真实原因**。
   * 此前 `.catch` 把失败直接吞成 `lessons: []` → 用户看到的是「暂无质检教训」这句
   * **业务空态文案**，而下拉/接口其实已经报错 —— 完全误导（既看不到错误也无法重试）。
   */
  const [lessonError, setLessonError] = useState('');
  const [lessonStats, setLessonStats] = useState({
    total: 0, by_kind: {} as Record<string, number>, dead_lessons: 0, used_total: 0,
  });
  /** 手动触发一次「重新拉取当前页」（删除/清空后） */
  const [reloadTick, setReloadTick] = useState(0);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  // 删除单条 / 清空本环节 / 清理 30 天前
  const [deleteTarget, setDeleteTarget] = useState<PromptLesson | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [clearKindOpen, setClearKindOpen] = useState(false);
  const [clearingKind, setClearingKind] = useState(false);
  const [clearOldOpen, setClearOldOpen] = useState(false);
  const [clearingOld, setClearingOld] = useState(false);

  const loadLessonStats = () => {
    memoryApi.lessonStats().then(setLessonStats).catch(() => {});
  };

  // C2（2026-09-23 收口）：原实现只有 `.finally` 没有 `.catch` —— memories/stats 任一
  // 失败会**静默落到空态**（显示「暂无记忆」，用户以为真没数据，且没有重试入口）。
  // 现捕获错误 → 整页错误态 + 可重试；取数抽成 reloadMain，「重试」才能真的重新取数。
  const reloadMain = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      const [list, d] = await Promise.all([
        // memoryApi.list() 已统一拆封为 Memory[]，这里不要再读 .memories，
        // 否则数组被当成信封对象解析，列表恒为空。
        memoryApi.list(),
        memoryApi.stats(),
      ]);
      setMemories(list);
      setStats(d);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reloadMain();
    setInsightsLoading(true);
    memoryApi.insights()
      .then(d => {
        const list = Array.isArray(d?.insights) ? d.insights : [];
        setInsights(list.map((x: unknown) => typeof x === 'string' ? x : JSON.stringify(x)));
      })
      .catch(() => {})
      .finally(() => setInsightsLoading(false));

    // 项目下拉（失败静默降级为空列表，不影响教训列表）
    projectsApi.list()
      .then(d => setProjects(d?.projects || []))
      .catch(() => setProjects([]));

    loadLessonStats();
  }, [reloadMain]);

  // 教训库单独拉取：任一筛选条件变化 → 回到第一页全量刷新
  useEffect(() => {
    let cancelled = false;
    setLessonLoading(true);
    memoryApi.lessons({
      kind: lessonKinds.join(',') || undefined,
      project: lessonProject || undefined,
      since: lessonSince || undefined,
      until: lessonUntil || undefined,
      q: lessonQ || undefined,
      limit: PAGE_SIZE,
      offset: 0,
    })
      .then(page => {
        if (cancelled) return;
        setLessonError('');
        setLessonPage(page);
        setLessons(page.lessons);
        setLessonOffset(page.lessons.length);
      })
      .catch((e) => {
        if (cancelled) return;
        // C3：不再把失败吞成空列表冒充「业务空态」，留下真实原因由界面呈现 + 可重试
        setLessonError(e instanceof Error ? e.message : String(e));
        setLessonPage({ total: 0, filtered: 0, by_kind: {}, dead_lessons: 0, lessons: [] });
        setLessons([]);
        setLessonOffset(0);
      })
      .finally(() => {
        if (!cancelled) setLessonLoading(false);
      });
    return () => { cancelled = true; };
  }, [lessonKinds, lessonProject, lessonSince, lessonUntil, lessonQ, reloadTick]);

  const toggleKind = (k: string) => {
    setLessonKinds(prev => (prev.includes(k) ? prev.filter(x => x !== k) : [...prev, k]));
  };

  const toggleExpand = (key: string) => {
    setExpanded(prev => ({ ...prev, [key]: !prev[key] }));
  };

  const handleLoadMore = async () => {
    setLessonLoading(true);
    try {
      const page = await memoryApi.lessons({
        kind: lessonKinds.join(',') || undefined,
        project: lessonProject || undefined,
        since: lessonSince || undefined,
        until: lessonUntil || undefined,
        q: lessonQ || undefined,
        limit: PAGE_SIZE,
        offset: lessonOffset,
      });
      setLessons(prev => [...prev, ...page.lessons]);
      setLessonOffset(prev => prev + page.lessons.length);
    } catch (e) {
      toast.error(t('memory.loadMoreFailed', { msg: e instanceof Error ? e.message : t('error.unknown') }));
    } finally {
      setLessonLoading(false);
    }
  };

  const handleDeleteLesson = async () => {
    if (!deleteTarget?.lesson_id) return;
    setDeleting(true);
    try {
      await memoryApi.deleteLesson(deleteTarget.lesson_id);
      toast.success(t('memory.lessonDeleted'));
      setDeleteTarget(null);
      setReloadTick(x => x + 1);
      loadLessonStats();
    } catch (e) {
      toast.error(t('memory.deleteFailed', { msg: e instanceof Error ? e.message : t('error.unknown') }));
    } finally {
      setDeleting(false);
    }
  };

  const handleClearLessons = async () => {
    if (lessonKinds.length !== 1) return;
    const kind = lessonKinds[0];
    setClearingKind(true);
    try {
      await memoryApi.clearLessons(kind);
      toast.success(t('memory.clearedKind', { kind: lessonKindLabel(kind) }));
      setClearKindOpen(false);
      setReloadTick(x => x + 1);
      loadLessonStats();
    } catch (e) {
      toast.error(t('memory.clearKindFailed', { msg: e instanceof Error ? e.message : t('error.unknown') }));
    } finally {
      setClearingKind(false);
    }
  };

  const handleClearOld = async () => {
    setClearingOld(true);
    try {
      await memoryApi.clearOld(30);
      // memoryApi.list() 已在这一层拆封成数组，可直接 setState。
      // 之前这里把整个响应对象 setMemories(d)，随后 memories.map 直接抛 TypeError。
      const [list, s] = await Promise.all([memoryApi.list(), memoryApi.stats()]);
      setMemories(list);
      setStats(s);
      toast.success(t('memory.clearedOld'));
    } catch (e) {
      console.error('Failed to clear', e);
      toast.error(t('memory.clearOldFailed', { msg: e instanceof Error ? e.message : t('error.unknown') }));
    } finally {
      setClearingOld(false);
      setClearOldOpen(false);
    }
  };

  /** 记忆类型 → 展示文案。后端类型集合比 i18n 现有键更宽，
   *  未知类型回退为原文，避免界面出现 `memory.type.xxx` 这种原始键名。 */
  const memTypeOf = (mem: Memory): MemoryType => (mem.mem_type || mem.type || 'insight');
  const memTypeLabel = (mem: Memory): string => {
    const key = `memory.type.${memTypeOf(mem)}`;
    const label = t(key);
    return label === key ? memTypeOf(mem) : label;
  };

  // 硬失败：memories / stats 一次都没取到 → 整页错误态（绝不用空态冒充「没数据」）
  if (loadError) {
    return (
      <div className="space-y-6 fade-in">
        <ErrorState
          title={t('project.loadingFailed')}
          description={sanitizeErrorMessage(loadError)}
          onRetry={reloadMain}
        />
      </div>
    );
  }

  // 加载态：沿用统计卡 5 列 + 卡片区块的形态，避免整页转圈造成布局跳变
  if (loading) {
    return (
      <div className="space-y-6 fade-in" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <div className="flex items-center justify-between">
          <Skeleton className="h-8 w-40" />
          <div className="flex gap-2">
            <Skeleton className="h-9 w-20" />
            <Skeleton className="h-9 w-24" />
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-20 rounded-lg" />
          ))}
        </div>
        <Skeleton className="h-64 rounded-lg" />
        <Skeleton className="h-48 rounded-lg" />
      </div>
    );
  }

  return (
    <div className="space-y-6 fade-in">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold text-ink-1">{t('memory.title')}</h2>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => window.location.reload()}>
            {t('common.refresh')}
          </Button>
          <Button variant="danger" onClick={() => setClearOldOpen(true)}>
            {t('memory.clearOld')}
          </Button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4">
        <StatCard label={t('memory.total')} value={stats.total} color="blue" />
        <StatCard label={t('memory.lessons')} value={stats.lessons} color="yellow" />
        <StatCard label={t('memory.successes')} value={stats.successes} color="green" />
        <StatCard label={t('memory.promptLessons')} value={stats.promptLessons ?? 0} color="purple" />
        <StatCard label={t('memory.deadLessons')} value={lessonStats.dead_lessons} color="gray" />
      </div>

      {/* 质检教训库：生成链路自动沉淀，驱动「不达标 → 改提示词重生成」 */}
      <Card title={t('memory.promptLessonsTitle')}>
        {/* 环节 chips 多选（7 类） */}
        {/* 保留原生：胶囊筛选 chip（rounded-full），Button 的 rounded-md 会改掉形状 */}
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <button
            onClick={() => setLessonKinds([])}
            className={`px-3 py-1 rounded-full text-xs border transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
              lessonKinds.length === 0
                ? 'bg-brand text-white border-brand'
                : 'bg-surface-2 text-ink-2 border-line hover:bg-line'
            }`}
          >
            {t('common.all')}
          </button>
          {LESSON_KINDS.map((k) => {
            const active = lessonKinds.includes(k);
            return (
              <button
                key={k}
                onClick={() => toggleKind(k)}
                className={`px-3 py-1 rounded-full text-xs border transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
                  active
                    ? 'bg-brand text-white border-brand'
                    : 'bg-surface-2 text-ink-2 border-line hover:bg-line'
                }`}
              >
                {lessonKindLabel(k)}
              </button>
            );
          })}
        </div>

        {/* 项目 / 时间区间 / 关键词检索 */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mb-3">
          <Select
            value={lessonProject}
            onChange={setLessonProject}
            options={[
              { value: '', label: t('memory.allProjects') },
              // ⚠️ 按 name 去重：Select 内部用 value 当 React key，同名项目会产生重复 key
              ...Array.from(new Map(projects.map((p) => [p.name, p])).values())
                .map((p) => ({ value: p.name, label: p.name })),
            ]}
          />
          <Input
            type="date"
            value={lessonSince}
            onChange={setLessonSince}
            placeholder={t('memory.dateFrom')}
          />
          <Input
            type="date"
            value={lessonUntil}
            onChange={setLessonUntil}
            placeholder={t('memory.dateTo')}
          />
          <Input
            value={lessonSearch}
            onChange={setLessonSearch}
            onEnter={() => setLessonQ(lessonSearch.trim())}
            placeholder={t('memory.searchPlaceholder')}
          />
        </div>

        {/* 汇总 + 清空本环节 */}
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
          <div className="text-xs text-ink-2">
            {t('memory.hitCount', { n: lessonPage.filtered })}
            {lessonPage.filtered !== lessonPage.total ? t('memory.totalInLibrary', { n: lessonPage.total }) : ''}
            {' · '}{t('memory.deadLessonsCount', { n: lessonPage.dead_lessons })}
            {' · '}{t('memory.recallTotal', { n: lessonStats.used_total })}
          </div>
          <Button
            variant="danger"
            size="sm"
            disabled={lessonKinds.length !== 1}
            onClick={() => setClearKindOpen(true)}
            title={lessonKinds.length === 1
              ? t('memory.clearKindTitle', { kind: lessonKindLabel(lessonKinds[0]) })
              : t('memory.selectOneKind')}
          >
            {t('memory.clearKind')}
          </Button>
        </div>

        {lessonError ? (
          /* 硬失败：教训库零数据且加载出错 → 区块错误态 + 重试（不再冒充「暂无质检教训」） */
          <ErrorState
            title={t('memory.lessonsLoadFailed')}
            description={lessonError}
            onRetry={() => setReloadTick((x) => x + 1)}
          />
        ) : lessons.length === 0 && !lessonLoading ? (
          <EmptyState
            icon={<BookOpen className="h-10 w-10" />}
            title={t('memory.noLessons')}
            description={t('memory.noLessonsDesc')}
            action={(
              <Button size="sm" variant="secondary" onClick={() => setReloadTick((x) => x + 1)}>
                {t('common.refresh')}
              </Button>
            )}
          />
        ) : (
          <div className="space-y-3">
            {lessons.map((l, i) => {
              const key = l.lesson_id || `${l.phash || ''}-${i}`;
              const isExpanded = !!expanded[key];
              const useCount = l.use_count ?? 0;
              const issues = l.issues || [];
              const terms = l.terms || [];
              return (
                <div key={key} className="p-4 bg-surface-2 border border-line rounded-lg">
                  {/* ⚠️ 必须允许换行：375 视口下左侧徽标 + 右侧时间/操作在同排会溢出视口 */}
                  <div className="flex flex-wrap items-start justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2 flex-wrap min-w-0">
                      <Badge variant="warning">{lessonKindLabel(l.kind)}</Badge>
                      {l.project ? (
                        <span className="text-xs text-ink-2">{l.project}</span>
                      ) : null}
                      {/* 召回次数徽标：0 次灰色弱化（死教训） */}
                      <span className={`text-xs px-2 py-0.5 rounded-full ${
                        useCount === 0
                          ? 'bg-line text-ink-2'
                          : 'bg-brand-subtle text-brand-hover'
                      }`}>
                        {t('memory.recallCount', { n: useCount })}
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs text-ink-3 whitespace-nowrap">
                        {l.ts ? new Date(l.ts).toLocaleString() : ''}
                      </span>
                      <Button
                        variant="link"
                        className="text-xs whitespace-nowrap"
                        onClick={() => toggleExpand(key)}
                      >
                        {isExpanded ? t('common.collapse') : t('common.details')}
                      </Button>
                      {/* text-danger 覆盖 link 变体的 text-brand：
                          Tailwind 按 theme.colors 键序产出，danger 在 brand 之后，同属性后者胜出 */}
                      <Button
                        variant="link"
                        className="text-xs whitespace-nowrap text-danger hover:text-danger-strong"
                        onClick={() => setDeleteTarget(l)}
                      >
                        {t('common.delete')}
                      </Button>
                    </div>
                  </div>

                  <ul className="list-disc list-inside space-y-1">
                    {issues.slice(0, 5).map((iss, j) => (
                      <li key={j} className="text-sm text-ink-1">{iss}</li>
                    ))}
                    {issues.length === 0 && l.reason ? (
                      <li className="text-sm text-ink-1">{l.reason}</li>
                    ) : null}
                  </ul>

                  {isExpanded && (
                    <div className="mt-3 pt-3 border-t border-line space-y-3">
                      <div>
                        <div className="text-xs font-medium text-ink-2 mb-1">{t('memory.promptOriginal')}</div>
                        <pre className="text-xs text-ink-1 bg-surface border border-line rounded p-2 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
                          {l.prompt || '—'}
                        </pre>
                      </div>
                      {issues.length > 0 && (
                        <div>
                          <div className="text-xs font-medium text-ink-2 mb-1">{t('memory.issues')}</div>
                          <ul className="list-disc list-inside space-y-1">
                            {issues.map((iss, j) => (
                              <li key={j} className="text-sm text-ink-1">{iss}</li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {terms.length > 0 && (
                        <div>
                          <div className="text-xs font-medium text-ink-2 mb-1">{t('memory.terms')}</div>
                          <div className="flex flex-wrap gap-2">
                            {terms.map((term) => (
                              <span key={term} className="text-xs px-2 py-0.5 bg-line rounded text-ink-2">
                                {term}
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
                        <div>
                          <span className="text-ink-2">{t('memory.scoreLabel')}</span>
                          <span className="text-ink-1">{typeof l.score === 'number' ? l.score : '—'}</span>
                        </div>
                        <div>
                          <span className="text-ink-2">{t('memory.matchSource')}</span>
                          <span className="text-ink-1">{lessonSourceLabel(l, t)}</span>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}

            {lessonLoading && <Loading size="sm" label={t('memory.loading')} />}
            {lessons.length < lessonPage.filtered && (
              <div className="flex justify-center pt-2">
                <Button variant="secondary" size="sm" onClick={handleLoadMore} loading={lessonLoading}>
                  {t('memory.loadMoreCount', { shown: lessons.length, total: lessonPage.filtered })}
                </Button>
              </div>
            )}
          </div>
        )}
      </Card>

      {/* Insights */}
      {!insightsLoading && insights.length > 0 && (
        <Card title={t('memory.insightTitle')}>
          <div className="space-y-2">
            {insights.slice(0, 5).map((ins, i) => (
              <p key={i} className="text-sm text-ink-2">
                {ins}
              </p>
            ))}
          </div>
        </Card>
      )}

      {/* Memories list */}
      <Card title={t('memory.listTitle')}>
        {memories.length === 0 ? (
          <EmptyState
            icon={<Brain className="h-10 w-10" />}
            title={t('memory.noMemories')}
            description={t('memory.noMemoriesDesc')}
            action={(
              <Button size="sm" variant="secondary" onClick={() => { void reloadMain(); }}>
                {t('common.refresh')}
              </Button>
            )}
          />
        ) : (
          <div className="space-y-3">
            {memories.slice(0, 20).map((mem) => (
              <div key={mem.mem_id || mem.id} className="p-4 bg-surface-2 rounded-lg">
                <div className="flex items-start justify-between mb-2">
                  <Badge variant={memTypeOf(mem) === 'lesson' ? 'warning' : memTypeOf(mem) === 'success' ? 'success' : 'info'}>
                    {memTypeLabel(mem)}
                  </Badge>
                  <span className="text-xs text-ink-3">{new Date(mem.created_at).toLocaleDateString()}</span>
                </div>
                <p className="text-ink-1 text-sm">{mem.content}</p>
                {(mem.tags || []).length > 0 && (
                  <div className="flex gap-2 mt-2">
                    {(mem.tags || []).map((tag) => (
                      <span key={tag} className="text-xs px-2 py-0.5 bg-line rounded text-ink-2">
                        #{tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 清理 30 天前记忆 */}
      <ConfirmDialog
        isOpen={clearOldOpen}
        onClose={() => (clearingOld ? undefined : setClearOldOpen(false))}
        onConfirm={handleClearOld}
        title={t('memory.clearOld')}
        danger
        loading={clearingOld}
        message={t('memory.clearOldConfirm')}
      />

      {/* 删除单条教训 */}
      <ConfirmDialog
        isOpen={!!deleteTarget}
        onClose={() => (deleting ? undefined : setDeleteTarget(null))}
        onConfirm={handleDeleteLesson}
        title={t('memory.deleteLessonTitle')}
        danger
        loading={deleting}
        message={deleteTarget
          ? t('memory.deleteLessonConfirm', { kind: lessonKindLabel(deleteTarget.kind) })
          : ''}
      />

      {/* 清空本环节 */}
      <ConfirmDialog
        isOpen={clearKindOpen}
        onClose={() => (clearingKind ? undefined : setClearKindOpen(false))}
        onConfirm={handleClearLessons}
        title={t('memory.clearKind')}
        danger
        loading={clearingKind}
        message={lessonKinds.length === 1
          ? t('memory.clearKindConfirm', { kind: lessonKindLabel(lessonKinds[0]), n: lessonPage.by_kind[lessonKinds[0]] ?? 0 })
          : ''}
      />
    </div>
  );
}

function StatCard({ label, value, color }: { label: string; value: number; color: string }) {
  const colorMap: Record<string, string> = {
    blue: 'bg-info-subtle text-brand',
    yellow: 'bg-warning-subtle text-warning-strong',
    green: 'bg-success-subtle text-success-strong',
    purple: 'bg-brand-subtle text-brand',
    gray: 'bg-surface-2 text-ink-2',
  };
  return (
    <Card>
      <div className="flex items-center gap-4">
        <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${colorMap[color] || colorMap.blue}`}>
          <span className="text-xl font-bold">{value}</span>
        </div>
        <span className="text-ink-2">{label}</span>
      </div>
    </Card>
  );
}
