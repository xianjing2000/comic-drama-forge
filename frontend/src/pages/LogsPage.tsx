import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { Badge, Button, Card, Input, Select } from '@/components/ui';
import { logsApi } from '@/api/client';
import LiveExecutionFeed from '@/components/LiveExecutionFeed';
import type { LogSource, LogsResponse } from '@/types';

/** 内存中最多保留多少行（防止长时间挂着拖垮浏览器） */
const MAX_KEEP = 2000;
const REFRESH_MS = 3000;

function fmtSize(n: number): string {
  if (!n) return '0 B';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function lineClass(line: string): string {
  if (line.startsWith('ERROR') || line.includes('ERROR:')) {
    return 'text-danger';
  }
  if (line.startsWith('WARNING') || line.includes('WARNING:')) {
    return 'text-warning-strong';
  }
  if (line.startsWith('DEBUG')) return 'text-ink-3';
  return 'text-ink-2';
}

/**
 * 后台服务日志
 *
 * 服务通过计划任务以后台进程运行，stdout/stderr 被重定向到磁盘上的日志文件，
 * 没有终端窗口可看 —— 这里把它接到 Web 上，支持实时跟随、按级别过滤与关键字搜索。
 *
 * 跟随策略：首屏按 tail 取尾部若干行，之后每 3 秒只请求 **since 之后的新增部分**
 * (AoT offset)，既省流量也省 CPU（后端是 seek 追加读，不重扫整个文件）。
 */
export function LogsPage() {
  const { t } = useApp();
  const [sources, setSources] = useState<LogSource[]>([]);
  const [source, setSource] = useState('serve');
  const [tail, setTail] = useState(300);
  const [level, setLevel] = useState('');
  const [query, setQuery] = useState('');
  const [auto, setAuto] = useState(true);
  const [lines, setLines] = useState<string[]>([]);
  const [meta, setMeta] = useState<LogsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // ⭐ 2026-10-09：执行流并入日志页（它本质就是结构化日志；概览页不再重复渲染）。

  //   日志页是全局页面、AppContext 里没有项目上下文 → 这里自带项目选择器，

  //   并记住上次选择（localStorage），下次进来直接看同一个项目。

  const [projList, setProjList] = useState<Array<{ key: string; name: string }>>([]);

  const [feedProject, setFeedProject] = useState<string>(

    () => localStorage.getItem('mjscxt.logs.feedProject') || '');

  useEffect(() => {

    void (async () => {

      try {

        const r = await fetch('/api/projects');

        const j = await r.json();

        const arr = (j?.projects || []).map((p: Record<string, unknown>) => ({

          key: String(p.dir_key || p.name || p.id || ''),

          name: String(p.name || p.dir_key || ''),

        })).filter((x: { key: string }) => x.key);

        setProjList(arr);

        setFeedProject((cur: string) => cur || arr[0]?.key || '');

      } catch { /* 拉不到项目不影响原始日志 */ }

    })();

  }, []);

  useEffect(() => {

    if (feedProject) localStorage.setItem('mjscxt.logs.feedProject', feedProject);

  }, [feedProject]);

  const offsetRef = useRef(0);
  const boxRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  const load = useCallback(async (incremental: boolean) => {
    setLoading(true);
    setError('');
    try {
      const res = await logsApi.tail({
        source,
        tail,
        since: incremental ? offsetRef.current : 0,
        q: query || undefined,
        level: level || undefined,
      });
      if (!res?.success) {
        setError(res?.error || t('wb.logs.loadFailed'));
        return;
      }
      offsetRef.current = res.offset ?? 0;
      setMeta(res);
      setLines(prev => {
        const next = incremental ? prev.concat(res.lines || []) : (res.lines || []);
        return next.length > MAX_KEEP ? next.slice(next.length - MAX_KEEP) : next;
      });
      if (stickRef.current) {
        requestAnimationFrame(() => {
          const el = boxRef.current;
          if (el) el.scrollTop = el.scrollHeight;
        });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [source, tail, query, level, t]);

  // 日志源清单（大小 / 更新时间）
  useEffect(() => {
    let alive = true;
    logsApi.sources()
      .then(r => { if (alive) setSources(r.sources || []); })
      .catch(() => { /* 清单拉不到不阻塞主体 */ });
    return () => { alive = false; };
  }, []);

  // 来源 / 行数 / 过滤条件变化 → 重新全量取，并把跟随偏移归零
  useEffect(() => {
    offsetRef.current = 0;
    void load(false);
  }, [load]);

  // 定时增量跟随
  useEffect(() => {
    if (!auto) return;
    const id = window.setInterval(() => { void load(true); }, REFRESH_MS);
    return () => window.clearInterval(id);
  }, [auto, load]);

  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    stickRef.current = atBottom;
  };

  const cur = sources.find(s => s.key === source);
  const counts = meta?.counts || {};

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">{t('wb.logs.title')}</h2>
          <p className="mt-1 max-w-3xl text-sm text-ink-3">{t('wb.logs.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant={auto ? 'primary' : 'secondary'} size="sm" onClick={() => setAuto(v => !v)}>
            {auto ? t('wb.logs.following') : t('wb.logs.paused')}
          </Button>
          <Button variant="secondary" size="sm" disabled={loading} onClick={() => void load(false)}>
            {t('wb.logs.refresh')}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setLines([])}>
            {t('wb.logs.clear')}
          </Button>
        </div>
      </div>

      {/* ===== 结构化执行流（原概览页的「执行流」，并入日志页）===== */}
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-sm font-medium text-ink-1">执行流 · 所属项目</span>
        <Select
          value={feedProject}
          onChange={setFeedProject}
          options={projList.length ? projList.map(p => ({ value: p.key, label: p.name }))
                                   : [{ value: '', label: '（暂无项目）' }]}
          className="min-w-[220px]"
        />
        <span className="text-xs text-ink-3">
          下方是结构化的生产事件（步骤开始/完成/失败/跳过 + 耗时 + 判断依据）；
          最下面的「日志正文」是服务进程的原始输出。
        </span>
      </div>
      {feedProject ? <LiveExecutionFeed project={feedProject} /> : null}

      {/* 控制条 */}
      <Card bodyClassName="p-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
          <Select
            label={t('wb.logs.source')}
            value={source}
            onChange={setSource}
            options={(sources.length ? sources : [{ key: source, label: source } as unknown as LogSource])
              .map(s => ({ value: s.key, label: s.label }))}
          />
          <Select
            label={t('wb.logs.level')}
            value={level}
            onChange={setLevel}
            options={[
              { value: '', label: t('wb.logs.levelAll') },
              { value: 'ERROR', label: 'ERROR' },
              { value: 'WARNING', label: 'WARNING' },
              { value: 'INFO', label: 'INFO' },
              { value: 'DEBUG', label: 'DEBUG' },
            ]}
          />
          <Select
            label={t('wb.logs.tail')}
            value={String(tail)}
            onChange={v => setTail(Number(v) || 300)}
            options={[
              { value: '100', label: '100' },
              { value: '300', label: '300' },
              { value: '1000', label: '1000' },
              { value: '2000', label: '2000' },
            ]}
          />
          <Input
            label={t('wb.logs.search')}
            value={query}
            onChange={setQuery}
            placeholder={t('wb.logs.searchPh')}
          />
        </div>
      </Card>

      {/* 状态信息 */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {cur && (
          <>
            <Badge variant="default">{t('wb.logs.size')}: {fmtSize(cur.size)}</Badge>
            <Badge variant="default">{t('wb.logs.modified')}: {cur.modified_at || '—'}</Badge>
          </>
        )}
        {meta?.encoding && <Badge variant="default">{t('wb.logs.encoding')}: {meta.encoding}</Badge>}
        {counts.ERROR ? <Badge variant="danger">ERROR: {counts.ERROR}</Badge> : null}
        {counts.WARNING ? <Badge variant="warning">WARNING: {counts.WARNING}</Badge> : null}
        {loading && <span className="text-ink-3">{t('wb.logs.loading')}</span>}
        {meta?.truncated && <Badge variant="warning">{t('wb.logs.truncated')}</Badge>}
      </div>

      {error && (
        <div className="rounded-lg border border-danger/40 bg-danger/5 p-3 text-sm text-danger">
          {error}
        </div>
      )}

      {/* 日志正文 */}
      <Card>
        <div
          ref={boxRef}
          onScroll={onScroll}
          className="max-h-[60vh] min-h-[320px] overflow-auto bg-black/40 p-4 font-mono text-xs leading-relaxed"
        >
          {lines.length === 0 && !loading && (
            <p className="text-ink-3">{t('wb.logs.empty')}</p>
          )}
          {lines.map((ln, i) => (
            <div key={`${i}-${ln.slice(0, 24)}`} className={`whitespace-pre-wrap break-all ${lineClass(ln)}`}>
              {ln}
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
