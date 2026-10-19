// 焦点环 class（与 components/ui/index.tsx 内的 FOCUS_RING 逐字一致）。
// 抽成共享常量供页面与抽出的子组件复用，避免多份漂移（E-5，2026-10-16）。
export const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas';
