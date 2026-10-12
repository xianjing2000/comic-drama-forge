/**
 * T12（2026-10-12）分镜质量口径：success / soft_accepted / blocked 三类分离。
 *
 * 背景（用户实测「逆天系统_全书」）：
 *   46 镜里 **17 镜（37%）是「软放行」** —— 质检判定不达标，但按
 *   `qc_config.storyboard_soft_qc`（默认 True）仍写入正式目录，其中数镜还带
 *   **非空的 critical_issues**（严重问题）。旧实现把软放行写成 success=True，
 *   于是 manifest 报 46/46、界面**全绿**，用户以为整集合格。
 *
 * 本模块是前端侧的唯一判据（纯函数、无 React 依赖、可单测），与后端
 * `app/storyboard_helpers._sb_quality_tally` **逐字同口径**：
 *   · soft_accepted —— 质检不达标但已入库，**不计入 success**；
 *   · qc_blocked    —— 质检阻断、未入库；
 *   · success       —— 真正通过质检并入库；
 *   · pending       —— 既无产物、也无「生成中」快照。
 */

export type SbQuality = 'success' | 'soft' | 'blocked' | 'pending';

/** 画布卡片 storyboard 对象的松散形态（后端 api_storyboard_canvas 的投影）。 */
export interface SbStoryboardLike {
  exists?: boolean;
  success?: boolean;
  blocked?: boolean;
  generating?: boolean;
  qc?: unknown;
  qc_gate?: unknown;
  qc_soft_accepted?: unknown;
  /** T25：canvas 自 2026-10-12 起在卡片顶层显式给出软放行事实（扁平布尔） */
  soft_accepted?: boolean;
  /** T25：产物是否已进正式目录（真通过 + 软放行） */
  delivered?: boolean;
  /** T25：卡片顶层严重问题清单（与 qc.critical_issues 同值） */
  critical_issues?: unknown;
  [k: string]: unknown;
}

/** 画布卡片（只用到这一小部分字段，其余原样透传）。 */
export interface SbCardLike {
  shot_id?: string | number;
  seq?: number;
  storyboard?: SbStoryboardLike | null;
  [k: string]: unknown;
}

const asDict = (v: unknown): Record<string, unknown> | null =>
  v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;

const asTextList = (v: unknown): string[] => {
  if (Array.isArray(v)) return v.map((x) => String(x ?? '').trim()).filter(Boolean);
  if (typeof v === 'string' && v.trim()) return [v.trim()];
  return [];
};

/**
 * 该镜是否为「软放行」（四处证据，与后端 _sb_soft_accepted / _sb_canvas_qc 同序）。
 *
 * T25：canvas 现在会在卡片**顶层**给出 `soft_accepted` / `qc_soft_accepted`
 * （修复前只透出 item["qc"] 原样，顶层字段全丢 → 实测「manifest 17 条、canvas 0 条」）。
 */
export function isSoftAccepted(sb?: SbStoryboardLike | null): boolean {
  const s = asDict(sb);
  if (!s) return false;
  if (s.soft_accepted === true) return true;        // T25 扁平字段（后端规范化投影）
  if (s.qc_soft_accepted === true) return true;
  const g = asDict(s.qc_gate);
  if (g?.soft_accepted === true) return true;
  const q = asDict(s.qc);
  return q?.soft_accepted === true || q?.soft_released === true;
}

/** 非空 critical_issues（软放行的「严重问题清单」）。 */
export function criticalIssuesOf(sb?: SbStoryboardLike | null): string[] {
  const s = asDict(sb);
  if (!s) return [];
  // T25：canvas 顶层的 critical_issues 优先（与 qc 内同值，但旧后端可能只给前者）
  const top = asTextList(s.critical_issues);
  if (top.length) return top;
  for (const src of [asDict(s.qc_gate), asDict(s.qc)]) {
    if (!src) continue;
    for (const key of ['critical_issues', 'critical_issue', 'critical_defects']) {
      const list = asTextList(src[key]);
      if (list.length) return list;
    }
  }
  return [];
}

/** 软放行原因（优先软放行专属字段，其次质检 reason / label 兜底）。 */
export function softReasonOf(sb?: SbStoryboardLike | null): string {
  const s = asDict(sb);
  if (!s) return '';
  const q = asDict(s.qc) ?? {};
  const g = asDict(s.qc_gate) ?? {};
  const pick = [q.soft_reason, s.soft_reason, q.reason, g.reason, q.label, g.label];
  for (const v of pick) {
    if (typeof v === 'string' && v.trim()) return v.trim();
  }
  return '';
}

/**
 * 单镜质量分类。判序与后端完全一致：**软放行优先**（同时存在 success/blocked 标记时
 * 也按软放行算，绝不回落到 success），其次阻断，再次真通过，最后未交付。
 */
export function shotQuality(sb?: SbStoryboardLike | null): SbQuality {
  const s = asDict(sb);
  if (!s) return 'pending';
  if (isSoftAccepted(sb)) return 'soft';
  if (s.blocked === true) return 'blocked';
  if (s.success === true && s.exists !== false) return 'success';
  if (s.exists === true) return 'success';
  return 'pending';
}

export interface SbSoftShot {
  seq: number;
  shotId: string;
  reason: string;
  /** 该镜的严重问题清单（为空表示「只是放行，没有 critical」） */
  critical: string[];
}

export interface SbQualitySummary {
  total: number;
  success: number;
  soft: number;
  blocked: number;
  pending: number;
  /** 软放行中带非空 critical_issues 的镜数（需显著提示） */
  critical: number;
  /** 软放行镜清单（按镜号排序，供「展开查看」） */
  softShots: SbSoftShot[];
  /** 其中带严重问题的镜（盾牌红标清单） */
  criticalShots: SbSoftShot[];
  /** 是否存在任何软放行（UI 据此决定是否亮出质量条） */
  hasSoft: boolean;
  /** 软放行占比（0-1，保留调用方格式化） */
  softRatio: number;
  /** T25：计数是否取自后端 summary（true = 与 manifest 同源同值，不再是前端自算） */
  countsFromBackend?: boolean;
}

const seqOf = (c: SbCardLike, i: number): number => {
  const s = Number(c?.seq);
  if (Number.isFinite(s) && s > 0) return s;
  const digits = String(c?.shot_id ?? '').replace(/\D/g, '');
  const n = Number(digits);
  return Number.isFinite(n) && n > 0 ? n : i + 1;
};

/**
 * 汇总一集的四类计数（前端与后端同口径）。
 *
 * @param cards 画布返回的 cards 数组
 */
export function summarizeStoryboardQuality(cards?: SbCardLike[] | null): SbQualitySummary {
  const list = Array.isArray(cards) ? cards : [];
  const softShots: SbSoftShot[] = [];
  let success = 0;
  let soft = 0;
  let blocked = 0;
  let pending = 0;
  let critical = 0;

  list.forEach((c, i) => {
    const sb = (c?.storyboard ?? null) as SbStoryboardLike | null;
    const q = shotQuality(sb);
    if (q === 'soft') {
      soft += 1;
      const ci = criticalIssuesOf(sb);
      if (ci.length) critical += 1;
      softShots.push({ seq: seqOf(c, i), shotId: String(c?.shot_id ?? ''), reason: softReasonOf(sb), critical: ci });
    } else if (q === 'blocked') blocked += 1;
    else if (q === 'success') success += 1;
    else pending += 1;
  });

  softShots.sort((a, b) => a.seq - b.seq);
  const total = list.length;
  return {
    total,
    success,
    soft,
    blocked,
    pending,
    critical,
    softShots,
    criticalShots: softShots.filter((s) => s.critical.length > 0),
    hasSoft: soft > 0,
    softRatio: total > 0 ? soft / total : 0,
  };
}

/** canvas summary 里的后端三类计数（T25 起由 /api/storyboard/canvas 提供）。 */
export interface SbBackendQualityCounts {
  success_count?: number;
  soft_accepted_count?: number;
  critical_issue_count?: number;
  qc_blocked_count?: number;
  delivered_count?: number;
}

/** 后端是否给出了三类计数（旧后端没有这些字段 → 前端回落自算，保持向后兼容）。 */
export function hasBackendQuality(summary?: SbBackendQualityCounts | null): boolean {
  const s = asDict(summary);
  if (!s) return false;
  return typeof s.soft_accepted_count === 'number' && typeof s.success_count === 'number';
}

/**
 * 以**后端 summary 的计数为准**（T25：canvas 与 manifest 顶层字段同源同值 ——
 * 这是「canvas soft 计数 == manifest soft_accepted_count」这条不变式的落点），
 * 明细（softShots / criticalShots）仍来自卡片，供清单展示。
 *
 * 后端没给（旧后端 / 老数据）时原样返回前端自算结果。
 */
export function mergeBackendQuality(
  local: SbQualitySummary,
  summary?: SbBackendQualityCounts | null,
): SbQualitySummary {
  if (!hasBackendQuality(summary)) return local;
  const b = summary as SbBackendQualityCounts;
  const success = Number(b.success_count ?? local.success);
  const soft = Number(b.soft_accepted_count ?? local.soft);
  const blocked = Number(b.qc_blocked_count ?? local.blocked);
  const critical = Number(b.critical_issue_count ?? local.critical);
  const total = local.total || success + soft + blocked;
  return {
    ...local,
    success,
    soft,
    blocked,
    critical,
    pending: Math.max(total - success - soft - blocked, 0),
    hasSoft: soft > 0,
    softRatio: total > 0 ? soft / total : 0,
    countsFromBackend: true,
  };
}
