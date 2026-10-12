import React from 'react';
import { createPortal } from 'react-dom';
import { useModalBehavior } from '@/hooks/useModalBehavior';
// 模块级 t：本文件是无 context 耦合的共享组件库，默认文案走 i18n，不引入 useApp()
import { t } from '@/i18n';

// ==========================================================================
// 全站唯一组件出口
// --------------------------------------------------------------------------
// ⚠️ 本文件是设计体系的落地层：**禁止出现 hex / rgba 字面量与 dark: 变体**，
//    颜色一律走 tailwind.config.js 映射出的语义色（surface / line / ink /
//    brand / accent / success / warning / danger / info / state-*）。
//    需要新颜色时先去 index.css 加 token，不要在这里写死色值。
// ⚠️ 交互元素必须带 focus-visible 焦点环（index.css 有 :focus-visible 兜底，
//    但组件自己给 ring 更可控，故显式声明）。
// ==========================================================================

/** 焦点环：鼠标点击不出现，键盘 Tab 必然可见 */
const FOCUS_RING = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';

/** 表单控件基线：36px 高、10px 圆角、聚焦转品牌色 */
const FIELD_BASE =
  'w-full rounded-md border bg-surface px-3 text-base text-ink-1 transition-colors placeholder:text-ink-3 ' +
  'disabled:cursor-not-allowed disabled:opacity-50';

const FIELD_OK = 'border-line hover:border-line-strong focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/25';
const FIELD_ERR = 'border-danger focus:border-danger focus:outline-none focus:ring-2 focus:ring-danger/25';

function FieldLabel({ label, children }: { label?: string; children: React.ReactNode }) {
  if (!label) return <>{children}</>;
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink-2">{label}</span>
      {children}
    </label>
  );
}

function FieldError({ error }: { error?: string }) {
  if (!error) return null;
  return <p className="mt-1 text-xs text-danger-strong">{error}</p>;
}

// ===================== 基础反馈 =====================

export function Loading({ size = 'md', label }: { size?: 'sm' | 'md' | 'lg'; label?: string }) {
  const sizes = { sm: 'w-4 h-4 border-2', md: 'w-8 h-8 border-[3px]', lg: 'w-12 h-12 border-4' };
  return (
    <div className="flex flex-col items-center justify-center gap-3 p-8" role="status" aria-live="polite">
      <div className={`${sizes[size]} animate-spin rounded-full border-line border-t-brand`} />
      {label && <span className="text-sm text-ink-2">{label}</span>}
    </div>
  );
}

/** 骨架屏：数据区块加载态占位，避免「白屏 → 内容」的跳变 */
export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-skeleton rounded-md bg-surface-2 ${className}`} aria-hidden="true" />;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  /** 空状态最需要「下一步该做什么」，所以留一个操作位而不是只给一句话 */
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      {icon && (
        <div className="mb-4 text-ink-3 [&>svg]:h-10 [&>svg]:w-10" aria-hidden="true">
          {/* 历史调用点传的是 emoji 字符串；新代码请直接传 Lucide/SVG 节点 */}
          {typeof icon === 'string' ? <span className="text-4xl leading-none">{icon}</span> : icon}
        </div>
      )}
      <h3 className="mb-2 text-lg font-medium text-ink-1">{title}</h3>
      {description && <p className="max-w-md text-sm text-ink-2">{description}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

/**
 * 排序控件（分镜拖拽排序的前端入口，2026-10-12）。
 *
 * 背景：后端 `POST /api/storyboard/shot/reorder` 与前端 `storyboardApi.reorder`
 * 都已存在，但**全站零调用点** —— 用户没有任何调整镜头顺序的手段。
 * 本组件只负责「手柄拖拽 + ↑/↓ 两个按钮」的交互与可用性判定：
 *   · 拖拽适合大跨度移动（分镜动辄 20+ 镜，一格一格点太慢）；
 *   · ↑/↓ 适合触屏与键盘用户 —— HTML5 拖拽在触屏上不可靠，无障碍场景更需要按钮，
 *     所以两套都提供，而不是只做拖拽。
 * 落盘（乐观更新 + 失败回滚）留在页面侧统一处理，组件不持有业务状态。
 * 文案走 i18n（本文件禁止硬编码中文文案），调用方需要时可用 labels 覆盖。
 */
export function ReorderControls({
  index,
  total,
  busy = false,
  dragActive = false,
  onMoveUp,
  onMoveDown,
  onDragStart,
  onDragEnd,
  labels,
  className = '',
}: {
  /** 当前项在列表中的下标（0 基） */
  index: number;
  /** 列表总长度：用于首/尾禁用 */
  total: number;
  /** 有排序请求在途：整体禁用，防重复提交 */
  busy?: boolean;
  /** 本项正在被拖拽（高亮手柄） */
  dragActive?: boolean;
  onMoveUp: () => void;
  onMoveDown: () => void;
  /** 传了才渲染拖拽手柄 */
  onDragStart?: (e: React.DragEvent) => void;
  onDragEnd?: (e: React.DragEvent) => void;
  labels?: { up?: string; down?: string; drag?: string };
  className?: string;
}) {
  const upLabel = labels?.up ?? t('sb.order.moveUp');
  const downLabel = labels?.down ?? t('sb.order.moveDown');
  const dragLabel = labels?.drag ?? t('sb.order.dragHandle');
  const canUp = index > 0 && !busy;
  const canDown = index >= 0 && index < total - 1 && !busy;
  const btn = `inline-flex h-6 w-6 items-center justify-center rounded-sm border border-line text-ink-2 transition-colors hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40 ${FOCUS_RING}`;
  return (
    <span className={`inline-flex items-center gap-1 ${className}`}>
      <button type="button" className={btn} onClick={onMoveUp} disabled={!canUp} title={upLabel} aria-label={upLabel}>
        <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6 15l6-6 6 6" />
        </svg>
      </button>
      <button type="button" className={btn} onClick={onMoveDown} disabled={!canDown} title={downLabel} aria-label={downLabel}>
        <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {onDragStart && (
        <span
          role="button"
          tabIndex={-1}
          draggable={!busy}
          onDragStart={onDragStart}
          onDragEnd={onDragEnd}
          title={dragLabel}
          aria-label={dragLabel}
          className={`inline-flex h-6 w-6 select-none items-center justify-center text-ink-3 hover:text-ink-1 ${
            dragActive ? 'text-brand' : ''
          } ${busy ? 'cursor-not-allowed opacity-40' : 'cursor-grab'}`}
        >
          {/* 六点手柄（Lucide grip-vertical 等价形）：本文件无图标依赖，内联 SVG 最省事 */}
          <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <circle cx="9" cy="6" r="1.4" />
            <circle cx="15" cy="6" r="1.4" />
            <circle cx="9" cy="12" r="1.4" />
            <circle cx="15" cy="12" r="1.4" />
            <circle cx="9" cy="18" r="1.4" />
            <circle cx="15" cy="18" r="1.4" />
          </svg>
        </span>
      )}
    </span>
  );
}

/** 错误态：给出错误码 + 原因 + 重试，而不是一句「加载失败」 */
export function ErrorState({
  title = t('common.loadFailed'),
  description,
  code,
  onRetry,
}: {
  title?: string;
  description?: React.ReactNode;
  code?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 text-center" role="alert">
      <svg className="h-10 w-10 text-danger" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <circle cx="12" cy="12" r="9" strokeWidth="1.6" />
        <path strokeLinecap="round" strokeWidth="1.8" d="M12 7.5v5.5M12 16.2v.6" />
      </svg>
      <h3 className="text-base font-medium text-ink-1">{title}</h3>
      {description && <p className="max-w-md text-sm text-ink-2">{description}</p>}
      {code && <code className="rounded-sm bg-surface-2 px-2 py-0.5 text-xs text-ink-3">{code}</code>}
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className={`mt-1 inline-flex h-9 items-center rounded-md border border-line bg-surface px-4 text-sm font-medium text-ink-1 transition-colors hover:bg-surface-2 ${FOCUS_RING}`}
        >
          {t('common.retry')}
        </button>
      )}
    </div>
  );
}

export function Badge({ children, variant = 'default' }: { children: React.ReactNode; variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' }) {
  const variants = {
    default: 'bg-surface-2 text-ink-2',
    success: 'bg-success-subtle text-success-strong',
    warning: 'bg-warning-subtle text-warning-strong',
    danger: 'bg-danger-subtle text-danger-strong',
    info: 'bg-info-subtle text-info-strong',
  };
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${variants[variant]}`}>
      {children}
    </span>
  );
}

export type ProductionStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped' | 'attention';

const STATUS_STYLE: Record<ProductionStatus, string> = {
  pending: 'bg-state-pending-subtle text-state-pending-strong',
  running: 'bg-state-running-subtle text-state-running-strong',
  done: 'bg-state-done-subtle text-state-done-strong',
  failed: 'bg-state-failed-subtle text-state-failed-strong',
  skipped: 'border border-dashed border-state-skipped bg-transparent text-state-skipped-strong',
  attention: 'bg-state-attention-subtle text-state-attention-strong',
};

/** 枚举 → i18n key（模块顶层不调 t()，渲染时再取文案） */
const STATUS_LABEL_KEY: Record<ProductionStatus, string> = {
  pending: 'state.pending',
  running: 'state.running',
  done: 'state.done',
  failed: 'state.failed',
  skipped: 'state.skipped',
  attention: 'state.attention',
};

/**
 * 生产状态徽标 —— 本项目最核心的状态表达（方案 §6.6）。
 * 漫剧生产链路有 pending/running/done/failed/skipped/attention 六种状态，
 * 此前全靠文字叙述，扫一眼看不出全局进度。
 */
export function StateBadge({
  status,
  label,
  onClick,
  className = '',
}: {
  status: ProductionStatus;
  label?: string;
  /** failed 可点击直接跳失败定位；传了 onClick 就渲染成 button */
  onClick?: () => void;
  className?: string;
}) {
  const base = 'inline-flex h-[22px] items-center gap-1.5 rounded-full px-2 text-xs font-medium';
  const body = (
    <>
      {status === 'running' && (
        <span className="h-1.5 w-1.5 animate-pulse-dot rounded-full bg-state-running" aria-hidden="true" />
      )}
      {status === 'attention' && (
        <svg className="h-3 w-3" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 3.6 22 20.4H2L12 3.6Z" />
        </svg>
      )}
      {label ?? t(STATUS_LABEL_KEY[status])}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        className={`${base} ${STATUS_STYLE[status]} transition-opacity hover:opacity-80 ${FOCUS_RING} ${className}`}
      >
        {body}
      </button>
    );
  }
  return <span className={`${base} ${STATUS_STYLE[status]} ${className}`}>{body}</span>;
}

export function Card({
  children,
  className = '',
  title,
  action,
  footer,
  /** 内容区留白：表格/图表类常需要 p-0 或更小的内边距 */
  bodyClassName = 'p-5',
}: {
  children: React.ReactNode;
  className?: string;
  title?: string;
  action?: React.ReactNode;
  footer?: React.ReactNode;
  bodyClassName?: string;
}) {
  return (
    <div className={`rounded-lg border border-line bg-surface shadow-xs ${className}`}>
      {(title || action) && (
        <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-4">
          {title && <h3 className="font-semibold text-ink-1">{title}</h3>}
          {action && <div className="shrink-0">{action}</div>}
        </div>
      )}
      <div className={bodyClassName}>{children}</div>
      {footer && (
        <div className="border-t border-line px-5 py-4">{footer}</div>
      )}
    </div>
  );
}

// ===================== 表单控件 =====================

export function Button({
  children,
  onClick,
  variant = 'primary',
  size = 'md',
  disabled = false,
  className = '',
  style,
  type = 'button',
  loading = false,
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  /** primary=实心深色主 CTA；brand=品牌色动作；secondary=白底描边；ghost=透明工具条；danger=删除类；link=行内 */
  variant?: 'primary' | 'brand' | 'secondary' | 'danger' | 'ghost' | 'link';
  size?: 'sm' | 'md' | 'lg';
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
  type?: 'button' | 'submit' | 'reset';
  /** 提交中：自动禁用并显示转圈，避免重复点击造成重复任务 */
  loading?: boolean;
  title?: string;
}) {
  const base = `inline-flex items-center justify-center gap-2 rounded-md font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${FOCUS_RING}`;
  const variants = {
    primary: 'bg-action text-white hover:bg-action/85',
    // 品牌=科技感主视觉：品牌→青渐变 + 极浅辉光（.progress-fill 见 index.css）
    brand: 'progress-fill text-white shadow-[0_2px_16px_rgb(var(--brand)/0.35)] hover:shadow-[0_2px_24px_rgb(var(--brand)/0.5)] hover:brightness-110',
    secondary: 'border border-line bg-surface text-ink-1 hover:bg-surface-2',
    danger: 'bg-danger text-white hover:bg-danger-strong',
    ghost: 'bg-transparent text-ink-2 hover:bg-surface-2 hover:text-ink-1',
    link: 'bg-transparent p-0 text-brand underline-offset-4 hover:text-brand-hover hover:underline',
  };
  // 高度 32 / 36 / 44（方案 §6.1）；link 走行内不设高度
  const sizes = {
    sm: 'h-8 px-3 text-sm',
    md: 'h-9 px-4 text-sm',
    lg: 'h-11 px-6 text-base',
  };
  const spin = { sm: 'w-3 h-3', md: 'w-3.5 h-3.5', lg: 'w-4 h-4' };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled || loading}
      style={style}
      title={title}
      aria-busy={loading || undefined}
      className={`${base} ${variants[variant]} ${variant === 'link' ? '' : sizes[size]} ${className}`}
    >
      {loading && (
        <span
          className={`${spin[size]} animate-spin rounded-full border-2 border-current border-t-transparent`}
          aria-hidden="true"
        />
      )}
      {children}
    </button>
  );
}

export function Input({
  value,
  onChange,
  placeholder,
  type = 'text',
  className = '',
  disabled = false,
  label,
  onEnter,
  autoFocus,
  error,
  suffix,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  className?: string;
  disabled?: boolean;
  label?: string;
  onEnter?: () => void;
  autoFocus?: boolean;
  /** 错误态：描边转红并在下方给出原因，别只靠 placeholder 传达约束 */
  error?: string;
  /** 尾部插槽（如「显示/隐藏密钥」按钮）：输入框自动让出右侧内边距，插槽绝对定位贴右 */
  suffix?: React.ReactNode;
}) {
  const input = (
    <input
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      autoFocus={autoFocus}
      aria-invalid={error ? true : undefined}
      onKeyDown={onEnter ? (e) => { if (e.key === 'Enter') onEnter(); } : undefined}
      className={`${FIELD_BASE} h-9 ${suffix ? 'pr-10' : ''} ${error ? FIELD_ERR : FIELD_OK} ${className}`}
    />
  );
  const field = suffix ? (
    <div className="relative">
      {input}
      <div className="absolute inset-y-0 right-0 flex items-center pr-1.5">{suffix}</div>
    </div>
  ) : (
    input
  );
  return (
    <FieldLabel label={label}>
      <>
        {field}
        <FieldError error={error} />
      </>
    </FieldLabel>
  );
}

export function Textarea({
  value,
  onChange,
  rows = 6,
  placeholder,
  className = '',
  label,
  mono = false,
  error,
  resize = true,
}: {
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
  className?: string;
  label?: string;
  mono?: boolean;
  error?: string;
  /** ⚠️ 必须走 prop 而不是 className 覆盖：resize-y 与 resize-none 同属性，
      谁生效取决于 Tailwind 产出顺序，靠 className 压不住 */
  resize?: boolean;
}) {
  const area = (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={rows}
      placeholder={placeholder}
      aria-invalid={error ? true : undefined}
      className={`${FIELD_BASE} ${resize ? 'resize-y' : 'resize-none'} py-2 ${error ? FIELD_ERR : FIELD_OK} ${mono ? 'font-mono text-sm' : ''} ${className}`}
    />
  );
  return (
    <FieldLabel label={label}>
      <>
        {area}
        <FieldError error={error} />
      </>
    </FieldLabel>
  );
}

/** 原生 <select> 此前 6 处各自手写样式，这里统一出口 */
export function Select({
  value,
  onChange,
  options,
  className = '',
  disabled = false,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  className?: string;
  disabled?: boolean;
  label?: string;
}) {
  const select = (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      className={`${FIELD_BASE} h-9 cursor-pointer ${FIELD_OK} ${className}`}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
  return <FieldLabel label={label}>{select}</FieldLabel>;
}

// ===================== 弹窗 =====================

const MODAL_SIZES = {
  sm: 'max-w-sm',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
  full: 'max-w-6xl',
};
export type ModalSize = keyof typeof MODAL_SIZES;

/**
 * 通用弹窗。
 *
 * ⚠️ 用 Portal 渲染到 document.body：只要祖先元素带有任何 transform / filter /
 * backdrop-filter（例如带入场动画的 .fade-in 页面根节点），position: fixed 就会
 * 以那个祖先为包含块，弹窗会被定位到超长页面的垂直中间 —— 也就是屏幕之外。
 * 挂到 body 上可彻底免疫这个问题。
 *
 * 本轮补齐（此前缺失导致的体验问题）：
 * - ESC 关闭、点遮罩关闭可关（closeOnBackdrop）
 * - 打开时**锁定背景滚动**（此前弹窗打开还能滚后面的长页面）
 * - **焦点陷阱 + 初始焦点 + 关闭后归还焦点**（键盘/读屏用户此前会迷路）
 * - role="dialog" / aria-modal / aria-labelledby；关闭按钮 aria-label
 * - size 档位（此前所有弹窗都写死 max-w-lg，长内容弹窗挤成一条）
 * - footer 操作区；入场动画
 *
 * 2026-09-23：层级由写死 z-[100] 收敛为 z-modal（方案 §5.3 六档规范）；
 * 遮罩 / 容器 / 描边全部改引用 token，与工作台自建弹层合并为唯一实现。
 */
export function Modal({
  isOpen,
  onClose,
  title,
  children,
  size = 'md',
  footer,
  closeOnBackdrop = true,
  closeOnEsc = true,
  preventClose = false,
  description,
}: {
  isOpen: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  size?: ModalSize;
  footer?: React.ReactNode;
  /** 误点遮罩就丢掉填写内容很恼人，表单类弹窗可传 false */
  closeOnBackdrop?: boolean;
  /** ESC 是否关闭。⚠️ 表单类弹窗挡了遮罩却留 ESC，输入照样会丢，两个要一起关 */
  closeOnEsc?: boolean;
  /** 提交中禁止关闭 */
  preventClose?: boolean;
  description?: string;
}) {
  const titleId = React.useId();
  const descId = React.useId();
  const containerRef = useModalBehavior({ isOpen, onClose, preventClose, closeOnEsc });

  if (!isOpen) return null;

  const node = (
    <div className="fixed inset-0 z-modal flex items-center justify-center p-4">
      <div
        className="absolute inset-0 animate-modal-backdrop bg-slate-900/50 backdrop-blur-sm"
        onClick={closeOnBackdrop && !preventClose ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        className={`glass-strong relative flex max-h-[90vh] w-full animate-modal-in flex-col rounded-xl border border-line shadow-lg ${MODAL_SIZES[size]}`}
      >
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-line px-6 py-4">
          <div className="min-w-0">
            <h2 id={titleId} className="break-words text-lg font-semibold text-ink-1">{title}</h2>
            {description && (
              <p id={descId} className="mt-1 text-sm text-ink-2">{description}</p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={preventClose}
            aria-label={t('common.close')}
            className={`shrink-0 rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink-1 disabled:opacity-40 ${FOCUS_RING}`}
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className="flex-1 overflow-y-auto p-6">{children}</div>
        {footer && (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-line px-6 py-4">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
  return typeof document === 'undefined' ? node : createPortal(node, document.body);
}

/**
 * 确认弹窗 —— 替代原生 `window.confirm()`
 *
 * 原生 confirm 的问题：阻塞主线程、样式无法定制（与深色主题完全脱节）、
 * 无法表达「危险操作」语义、不能显示处理中状态。项目里原本有 3 处在用。
 */
export function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  message,
  confirmText = t('common.ok'),
  cancelText = t('common.cancel'),
  danger = false,
  loading = false,
}: {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void | Promise<void>;
  title: string;
  message?: React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
  loading?: boolean;
}) {
  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={title}
      size="sm"
      preventClose={loading}
      closeOnBackdrop={!danger}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={loading}>{cancelText}</Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            onClick={onConfirm}
            loading={loading}
            className="min-w-[5rem]"
          >
            {confirmText}
          </Button>
        </>
      }
    >
      {message ? (
        <div className="text-sm leading-6 text-ink-2">{message}</div>
      ) : null}
    </Modal>
  );
}
