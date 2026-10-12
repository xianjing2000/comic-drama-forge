// ============================================
// 错误文案工具（2026-10-12，t27）
// ============================================
//
// 为什么独立成模块：t26 把脱敏函数写在 ProjectWorkbenchPage.tsx 内部，但
// 「错误展示不向用户泄露本机绝对路径 / 实现细节」是全前端的约定 —— 交付页
// （OutputReviewTab）、质检面板（QualityPanel）、执行流（LiveExecutionFeed）
// 都需要同一份实现。复制多份必然漂移，所以收敛到这里，页面只 import。

/**
 * 后端异常消息脱敏（t26 实现原样迁移，行为一字不改）。
 *
 * 用户反馈工作台的失败态「太丑」—— 实测截图里整屏都是一行内部绝对路径，例如
 *   「第1集尚未生成：C:\Users\liujianghua\AppData\Roaming\mjscxt-desktop\
 *     mjscxt-data\output\scripts\逆天系统_全书\第1集.json」
 * 这类路径对用户零价值（他不需要知道自己的 AppData 在哪），还挤爆版面。
 * 这里统一把 Windows / POSIX 绝对路径替换成占位符，只保留可读的语义部分。
 */
export function sanitizeErrorMessage(msg: string): string {
  if (!msg) return '';
  return String(msg)
    // Windows 绝对路径（含盘符），保留到空白或中文标点为止
    .replace(/[A-Za-z]:\\[^\s：:，,；;）)]+/g, '[本地文件路径]')
    // POSIX 绝对路径
    .replace(/\/(?:[^\s：:，,；;)\/]+\/)+[^\s：:，,；;）)]*/g, '[本地文件路径]')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 300);
}

/**
 * 剥掉实现细节（t26 的 sanitizeError 逻辑，t27 收敛到此处）。
 *
 * 与 sanitizeErrorMessage 的分工：这个函数处理「后端把 Python traceback / 模块名 /
 * import 语句拼进 error」这一类，sanitizeErrorMessage 处理「错误文案里带本机路径」。
 * 两者叠加才是用户能看的错误行。
 */
function stripImplDetails(msg: string): string {
  let text = String(msg || '').trim();
  if (!text) return '';
  const tb = text.indexOf('Traceback (most recent call last)');
  if (tb !== -1) text = text.slice(0, tb).trim();
  const lines = text.split(/\r?\n/).filter(Boolean);
  text = (lines[lines.length - 1] || '').trim();
  return text
    .replace(/File\s+"[^"]*",\s*line\s*\d+/g, '')
    .replace(/\s*from\s+'[^']*'/g, '')
    .replace(/\s*\([^()]*\.py[^()]*\)/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

/** 结构化错误信息：文案（已脱敏）+ 后端错误码 + HTTP 状态 */
export interface ErrorInfo {
  /** 可安全展示的文案（已脱敏；调用方给了 fallback 时不会为空） */
  message: string;
  /** 后端返回的错误码（如 deliverable_stale）；无则为空串 */
  code: string;
  /** HTTP 状态码；非 HTTP 失败（如本地异常）为 0 */
  status: number;
}

/**
 * 把 catch 到的任意值整理成界面可直接用的 { message, code, status }。
 *
 * 为什么需要 code：t27 起后端对「过期成片被验收」返回 HTTP 409 +
 * code="deliverable_stale"。界面要按状态分流（禁用验收按钮 / 引导重新合成），
 * 而不是靠匹配中文文案 —— 文案一改判断就失效。
 *
 * 这里用结构化判断（读 err.message/code/status）而不是 instanceof ApiError：
 * 既避免 utils ← api/client 的循环依赖，也能兼容 fetch 之外的错误源。
 */
export function describeError(err: unknown, fallback = ''): ErrorInfo {
  const e = err as { message?: unknown; code?: unknown; status?: unknown } | null | undefined;
  const rawMessage = typeof e?.message === 'string'
    ? e.message
    : (typeof err === 'string' ? err : '');
  const rawCode = typeof e?.code === 'string' ? e.code : '';
  const status = typeof e?.status === 'number' ? e.status : 0;
  const stripped = stripImplDetails(rawMessage);
  const message = sanitizeErrorMessage(stripped)
    || sanitizeErrorMessage(fallback)
    || fallback
    || '';
  return { message, code: rawCode, status };
}
