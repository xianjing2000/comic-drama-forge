import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { promptsApi, type PromptTemplateSummary } from '@/api/client';
import { Badge, Button, Card, ConfirmDialog, EmptyState, ErrorState, Skeleton, Textarea } from '@/components/ui';
import { Copy, FileText, RefreshCw } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';

// 焦点环：与 components/ui/index.tsx 的 FOCUS_RING 逐字一致（同 ProjectWorkbenchPage 的抄法）
const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

/** 编辑器里的模板视图（列表项直接摊平；列表缺项时回退 GET /prompts/get 单拉） */
interface PromptDetail {
  name: string;
  title: string;
  /** 文件原文（含 # 头注释块）—— 后端 load() 送模型前会剥掉头注释，编辑器所见即所存 */
  text: string;
  variables: string[];
  source: string;
}

/**
 * 写剪贴板：优先 async Clipboard API；http 环境或旧内核没有/被拒时
 * 退化为隐藏 textarea + execCommand('copy')。
 */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* 权限被拒 / 非安全上下文 → 走降级 */
  }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/**
 * 提示词模板管理（2026-10-07，借鉴 Moha 的提示词管理布局）。
 *
 * 布局：左侧模板列表（title + 来源徽标：已自定义/默认 + 字数），
 * 右侧编辑区（标题、变量 chips（点击复制占位符）、大 textarea、
 * 底部「恢复默认」「保存」+ 字数统计）。
 *
 * 后端：app/prompt_templates.py（用户覆盖 > 出厂默认 > 代码内兜底）。
 * 保存缺变量会被后端 400 拒绝，错误文案经 readError 透传到 toast。
 */
export function PromptManagerTab() {
  const { t } = useApp();
  const toast = useToast();

  const [templates, setTemplates] = useState<PromptTemplateSummary[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState('');

  const [selected, setSelected] = useState<PromptDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [draft, setDraft] = useState('');
  const [dirty, setDirty] = useState(false);

  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);

  // 待确认动作：切换模板（有未保存修改）/ 恢复默认
  const [switchTarget, setSwitchTarget] = useState<string | null>(null);
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);

  // openTemplate 是 useCallback（deps 不含 selected/dirty，避免每次键入都换新引用
  // 连带 useEffect 重拉列表）—— 这里用 ref 跟踪「当前选中名 / 是否有未保存修改」，
  // 供「保存后的列表刷新」判断要不要保留正在输入的草稿（见 openTemplate 内注释）。
  const dirtyRef = useRef(false);
  const selectedNameRef = useRef<string | null>(null);

  /**
   * 打开某模板：优先用列表里已带的数据（list 返回的 text 就是文件原文），缺项再单拉 get。
   * preserveDraftIfDirty=true（仅保存后的静默刷新用）：用户此刻还在往编辑框里打字时，
   * 只对齐基线元数据、不覆盖草稿 —— 否则 save+刷新期间键入的内容会被服务端文本静默吞掉。
   */
  const openTemplate = useCallback(async (
    name: string,
    list?: PromptTemplateSummary[],
    opts?: { preserveDraftIfDirty?: boolean }
  ) => {
    const preserveDraft = Boolean(opts?.preserveDraftIfDirty);
    const item = (list || []).find((x) => x.name === name);
    const sameName = selectedNameRef.current === name;
    selectedNameRef.current = name;
    if (item) {
      setSelected({
        name: item.name,
        title: item.title || item.name,
        text: item.text || '',
        variables: Array.isArray(item.variables) ? item.variables : [],
        source: item.source || 'default',
      });
      if (preserveDraft && sameName && dirtyRef.current) return;
      setDraft(item.text || '');
      setDirty(false);
      dirtyRef.current = false;
      return;
    }
    setDetailLoading(true);
    try {
      const d = await promptsApi.get(name);
      setSelected({
        name: d.name || name,
        title: d.title || name,
        text: d.text || '',
        variables: Array.isArray(d.variables) ? d.variables : [],
        source: d.source || 'default',
      });
      if (preserveDraft && sameName && dirtyRef.current) return;
      setDraft(d.text || '');
      setDirty(false);
      dirtyRef.current = false;
    } catch (err) {
      toast.error(err instanceof Error ? err.message : t('prompts.loadFailed'));
    } finally {
      setDetailLoading(false);
    }
  }, [t, toast]);

  /**
   * 拉模板列表并选中模板。
   * initial=true：首屏加载，失败时整块展示 ErrorState（可重试）；
   * 否则为保存/恢复后的静默刷新 —— 失败只 console.warn，保留旧列表，
   * 不能因为列表刷新失败把刚保存成功的编辑区整个顶掉。
   */
  const loadList = useCallback(async (
    keepName?: string | null,
    opts?: { initial?: boolean; preserveDraft?: boolean }
  ) => {
    const initial = Boolean(opts?.initial);
    if (initial) {
      setListLoading(true);
      setListError('');
    }
    try {
      const d = await promptsApi.list();
      const list = Array.isArray(d?.prompts) ? d.prompts : [];
      setTemplates(list);
      const keep = keepName && list.some((x) => x.name === keepName) ? keepName : null;
      const nextName = keep || list[0]?.name || '';
      if (nextName) {
        await openTemplate(nextName, list, { preserveDraftIfDirty: Boolean(opts?.preserveDraft) });
      } else {
        selectedNameRef.current = null;
        setSelected(null);
        setDraft('');
        setDirty(false);
        dirtyRef.current = false;
      }
    } catch (err) {
      if (initial) {
        setListError(err instanceof Error ? err.message : t('prompts.loadFailed'));
      } else {
        // eslint-disable-next-line no-console
        console.warn('[prompts] 静默刷新模板列表失败（保留旧列表）', err);
      }
    } finally {
      if (initial) setListLoading(false);
    }
  }, [openTemplate, t]);

  useEffect(() => {
    void loadList(null, { initial: true });
  }, [loadList]);

  /** 点击左侧列表项：有未保存修改先弹确认，确认后直接丢弃并切换 */
  const handleSelectClick = (name: string) => {
    if (selected?.name === name) return;
    if (dirty) {
      setSwitchTarget(name);
      return;
    }
    void openTemplate(name, templates);
  };

  const handleSave = async () => {
    if (!selected || saving) return;
    setSaving(true);
    try {
      await promptsApi.save(selected.name, draft);
      toast.success(t('prompts.saved', { name: selected.title }));
      setDirty(false);
      dirtyRef.current = false;
      // 重新拉列表：来源徽标（默认 → 已自定义）与字数都要跟着变；
      // 保留当前选中项（keepName），用新数据重开编辑区（selected.text 归位）。
      // preserveDraft：刷新期间用户若还在打字，不吞草稿。
      await loadList(selected.name, { preserveDraft: true });
    } catch (err) {
      // 后端 400（缺变量占位符）的可读文案已被 readError 提取进 Error.message
      toast.error(t('prompts.saveFailed', { err: err instanceof Error ? err.message : t('error.unknown') }));
    } finally {
      setSaving(false);
    }
  };

  const doReset = async () => {
    if (!selected || resetting) return;
    setResetting(true);
    try {
      await promptsApi.reset(selected.name);
      toast.success(t('prompts.resetDone', { name: selected.title }));
      await loadList(selected.name);
    } catch (err) {
      toast.error(t('prompts.resetFailed', { err: err instanceof Error ? err.message : t('error.unknown') }));
    } finally {
      setResetting(false);
      setResetConfirmOpen(false);
    }
  };

  /** 变量 chip 点击：复制占位符（带花括号，粘进正文即可用） */
  const handleCopyVariable = async (v: string) => {
    const placeholder = `{${v}}`;
    const ok = await copyToClipboard(placeholder);
    if (ok) toast.success(t('prompts.copied', { name: placeholder }));
    else toast.error(t('prompts.copyFailed'));
  };

  // 「恢复默认」在两种情况下有意义：已是用户覆盖（删覆盖回到出厂），
  // 或本地有未保存修改（拉回默认原文，等价「放弃修改」）。其余时候是 no-op，禁用。
  const canReset = selected != null && (dirty || selected.source === 'override');
  const switchTargetTitle =
    templates.find((x) => x.name === switchTarget)?.title || switchTarget || '';

  // ===== 首屏加载骨架（对齐 AIVaultPage 的骨架风格）=====
  if (listLoading && templates.length === 0) {
    return (
      <div className="space-y-3" role="status" aria-live="polite" aria-label={t('prompts.loading')}>
        <div className="space-y-2">
          <Skeleton className="h-6 w-44" />
          <Skeleton className="h-4 w-72" />
        </div>
        <Skeleton className="h-[480px] rounded-lg" />
      </div>
    );
  }

  // ===== 首屏失败：整块 ErrorState（可重试）=====
  if (listError && templates.length === 0) {
    return (
      <div className="space-y-3">
        <div>
          <h3 className="text-lg font-semibold text-ink-1">{t('prompts.title')}</h3>
          <p className="text-sm text-ink-2 mt-1">{t('prompts.subtitle')}</p>
        </div>
        <ErrorState
          title={t('prompts.loadFailed')}
          description={listError}
          onRetry={() => void loadList(selected?.name || null, { initial: true })}
        />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* 区块头 */}
      <div>
        <h3 className="text-lg font-semibold text-ink-1">{t('prompts.title')}</h3>
        <p className="text-sm text-ink-2 mt-1">{t('prompts.subtitle')}</p>
      </div>

      <Card bodyClassName="p-0">
        {templates.length === 0 ? (
          <EmptyState
            icon={<FileText className="h-10 w-10" />}
            title={t('prompts.empty')}
            description={t('prompts.emptyHint')}
          />
        ) : (
          <div className="flex flex-col md:flex-row">
            {/* ===== 左：模板列表（Moha 布局）===== */}
            <aside className="md:w-72 shrink-0 border-b md:border-b-0 md:border-r border-line flex flex-col">
              <div className="flex items-center justify-between px-4 py-3 border-b border-line shrink-0">
                <span className="text-sm font-medium text-ink-1">{t('prompts.listTitle')}</span>
                <Badge variant="default">{t('prompts.count', { n: templates.length })}</Badge>
              </div>
              <nav className="flex-1 overflow-y-auto p-2 space-y-1 max-h-[240px] md:max-h-[560px]" aria-label={t('prompts.listTitle')}>
                {templates.map((tpl) => {
                  const isOverride = tpl.source === 'override';
                  const active = selected?.name === tpl.name;
                  return (
                    <button
                      key={tpl.name}
                      type="button"
                      onClick={() => handleSelectClick(tpl.name)}
                      aria-current={active || undefined}
                      className={`w-full text-left px-3 py-2.5 rounded-lg transition-colors ${FOCUS_RING} ${
                        active ? 'bg-brand-subtle text-brand' : 'text-ink-1 hover:bg-surface-2'
                      }`}
                    >
                      <span className="block text-sm font-medium truncate">{tpl.title || tpl.name}</span>
                      <span className="mt-1 flex items-center gap-1.5">
                        <Badge variant={isOverride ? 'warning' : 'default'}>
                          {isOverride ? t('prompts.overrideBadge') : t('prompts.defaultBadge')}
                        </Badge>
                        <span className="text-xs text-ink-3 tabular-nums">
                          {t('prompts.wordCount', { n: tpl.length })}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </nav>
            </aside>

            {/* ===== 右：编辑区 ===== */}
            <section className="flex-1 min-w-0 p-5 space-y-3">
              {detailLoading && (
                <div className="text-sm text-ink-2" role="status" aria-live="polite">
                  {t('prompts.loading')}
                </div>
              )}
              {!selected && !detailLoading && (
                <EmptyState
                  icon={<FileText className="h-10 w-10" />}
                  title={t('prompts.empty')}
                  description={t('prompts.emptyHint')}
                />
              )}
              {selected && (
                <>
                  {/* 标题行 + 徽标 */}
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h4 className="text-base font-semibold text-ink-1 flex items-center gap-2">
                        <FileText className="h-4 w-4 shrink-0 text-ink-3" />
                        <span className="truncate">{selected.title}</span>
                      </h4>
                      <p className="text-xs text-ink-3 mt-0.5 font-mono truncate">{selected.name}</p>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {dirty && <Badge variant="warning">{t('prompts.unsavedBadge')}</Badge>}
                      <Badge variant={selected.source === 'override' ? 'warning' : 'default'}>
                        {selected.source === 'override' ? t('prompts.overrideBadge') : t('prompts.defaultBadge')}
                      </Badge>
                    </div>
                  </div>

                  {/* 变量 chips（点击复制占位符） */}
                  {selected.variables.length > 0 && (
                    <div>
                      <div className="flex items-baseline gap-2 mb-1.5 flex-wrap">
                        <span className="text-xs font-medium text-ink-2">{t('prompts.variables')}</span>
                        <span className="text-xs text-ink-3">{t('prompts.variablesHint')}</span>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {selected.variables.map((v) => (
                          <button
                            key={v}
                            type="button"
                            onClick={() => void handleCopyVariable(v)}
                            title={t('prompts.copyHint', { name: `{${v}}` })}
                            className={`inline-flex items-center gap-1 rounded-md border border-line bg-surface-2 px-2 py-1 font-mono text-xs text-ink-1 transition-colors hover:border-brand hover:text-brand ${FOCUS_RING}`}
                          >
                            <Copy className="h-3 w-3" />
                            {`{${v}}`}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* 正文编辑器（# 头注释一并回显：所见即所存） */}
                  <Textarea
                    label={t('prompts.editorLabel')}
                    value={draft}
                    onChange={(v) => {
                      setDraft(v);
                      const d = v !== selected.text;
                      setDirty(d);
                      dirtyRef.current = d;
                    }}
                    rows={18}
                    mono
                    className="text-sm"
                  />

                  {/* 底部：字数统计 + 操作按钮 */}
                  <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
                    <span className="text-xs text-ink-3 tabular-nums">
                      {t('prompts.wordCount', { n: draft.length })}
                    </span>
                    <div className="flex items-center gap-2">
                      {!canReset && (
                        <span className="text-xs text-ink-3">{t('prompts.resetDisabledHint')}</span>
                      )}
                      <Button
                        variant="secondary"
                        onClick={() => setResetConfirmOpen(true)}
                        disabled={!canReset || resetting}
                      >
                        <RefreshCw className="h-4 w-4" />
                        {t('prompts.reset')}
                      </Button>
                      <Button
                        variant="brand"
                        onClick={handleSave}
                        loading={saving}
                        disabled={!dirty}
                      >
                        {t('prompts.save')}
                      </Button>
                    </div>
                  </div>
                </>
              )}
            </section>
          </div>
        )}
      </Card>

      {/* 切换模板确认（有未保存修改） */}
      <ConfirmDialog
        isOpen={switchTarget !== null}
        onClose={() => setSwitchTarget(null)}
        onConfirm={() => {
          const name = switchTarget;
          setSwitchTarget(null);
          if (name) void openTemplate(name, templates);
        }}
        title={t('prompts.switchConfirmTitle')}
        message={t('prompts.switchConfirm', { name: switchTargetTitle })}
      />

      {/* 恢复默认确认（危险操作：删除用户覆盖） */}
      <ConfirmDialog
        isOpen={resetConfirmOpen}
        onClose={() => (resetting ? undefined : setResetConfirmOpen(false))}
        onConfirm={doReset}
        title={t('prompts.restoreConfirmTitle')}
        danger
        loading={resetting}
        confirmText={t('prompts.reset')}
        message={selected ? t('prompts.restoreConfirm', { name: selected.title }) : null}
      />
    </div>
  );
}
