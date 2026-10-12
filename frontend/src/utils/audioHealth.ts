// ============================================================================
// 音频体检的纯逻辑（t9）
// ----------------------------------------------------------------------------
// 为什么从组件里抽出来：这些判据（静音口径、门禁处置、类目状态）是**与后端契约
//   逐字对齐**的东西，必须能被单独验证 —— 留在 .tsx 里既无法单测，也容易在下一
//   次改 UI 时被顺手改掉而没人发现。
//
// 口径来源（后端，逐字对齐，改动前先改那边）：
//   · app/routes/qc.py::_audio_qc_candidates → 五类产物 kind：
//       line / voice_bank / merged / mix / final
//   · app/routes/qc.py::api_qc_triage → audio.checked / audio.scanned / audio.legend
//       / audio.failed_count / audio.failed[]（**最多 20 条**，故用 failed_count 计总数）
//   · app/audio_qc.py: NEAR_SILENT_MEAN_DB=-50dB、HARD_SILENT_RATIO=0.15
//   · app/pipeline.py::step_final → MJSCXT_FINAL_AUDIO_GATE 默认**只记录不阻断**
// ============================================================================
import type { QCTriageAudioFailed, QCTriageResult } from '@/api/client';
import type { Deliverable } from '@/types';

/** 扫描类目：与后端 kind 逐字一致，顺序即展示顺序。 */
export const AUDIO_KINDS = ['line', 'voice_bank', 'merged', 'mix', 'final'] as const;
/** kind → locales 键后缀（audio.triageKind*）。 */
export const KIND_KEY: Record<string, string> = {
  line: 'Line', voice_bank: 'VoiceBank', merged: 'Merged', mix: 'Mix', final: 'Final',
};
/** 静音判据（与 audio_qc 同口径）：平均电平低于此值或 有声占比低于 SILENT_RATIO 即「近乎无声」。 */
export const SILENT_MEAN_DB = -50;
export const SILENT_RATIO = 0.15;

export function isSilentFail(f: QCTriageAudioFailed): boolean {
  // 检查异常（source=error）不是「静音」，计入「异常」——否则会把探针故障说成音频缺陷
  if (String(f.source || '') === 'error') return false;
  const m = f.metrics || {};
  const mean = typeof m.mean_db === 'number' ? m.mean_db : null;
  const ratio = typeof m.speech_ratio === 'number' ? m.speech_ratio : null;
  if (mean !== null && mean < SILENT_MEAN_DB) return true;
  if (ratio !== null && ratio < SILENT_RATIO) return true;
  const text = [...(f.reason || []), ...(f.critical || [])].join(' ');
  return /无声|静音|silent/i.test(text);
}

/** 取该集「最新」的一条交付登记（同集可能多次登记，按时间兜底）。 */
export function pickDeliverable(items: Deliverable[] | undefined, ep: number): Deliverable | null {
  const rows = (items || []).filter((i) => Number(i.episode_no) === Number(ep));
  if (!rows.length) return null;
  return rows.reduce((best, cur) => {
    const bt = Date.parse(String(best.updated_at || best.created_at || '')) || 0;
    const ct = Date.parse(String(cur.updated_at || cur.created_at || '')) || 0;
    return ct >= bt ? cur : best;
  });
}

export interface AudioSummary {
  /** 后端 audio.checked（扫描总数）——**原样直传**，不重算 */
  checked: number;
  /** 后端 audio.failed_count（不合格总数，可能大于 listedCount） */
  failedCount: number;
  /** 后端 audio.failed[] 实际条数（后端单次最多 20 条） */
  listedCount: number;
  /** 未列出的条数（failedCount - listedCount） */
  truncated: number;
  normalCount: number;
  silentCount: number;
  abnormalCount: number;
  /** 扫描数为 0 的类目（无产物可体检） */
  missingKinds: string[];
  /** 后端 audio.scanned 原样（数字与后端逐字一致） */
  scanned: Record<string, number>;
  /** 后端 audio.legend 原样（类目中文名） */
  legend: Record<string, string>;
  failed: QCTriageAudioFailed[];
  failedByKind: Record<string, QCTriageAudioFailed[]>;
  mode: string;
}

export function summarizeAudioHealth(triage: QCTriageResult | null): AudioSummary {
  const audio = triage?.audio || {};
  const scanned: Record<string, number> = audio.scanned || {};
  const legend: Record<string, string> = audio.legend || {};
  const failed = audio.failed || [];
  const checked = Number(audio.checked || 0);
  const failedCount = Number(audio.failed_count ?? failed.length);
  const silentCount = failed.filter(isSilentFail).length;
  const abnormalCount = Math.max(0, failed.length - silentCount);
  const failedByKind: Record<string, QCTriageAudioFailed[]> = {};
  for (const f of failed) {
    const k = String(f.kind || 'other');
    (failedByKind[k] = failedByKind[k] || []).push(f);
  }
  return {
    checked,
    failedCount,
    listedCount: failed.length,
    truncated: Math.max(0, failedCount - failed.length),
    // 正常 = 总数 − 不合格；静音/异常按失败项归类，三者之和 = 总数（明细不截断时）
    normalCount: Math.max(0, checked - failedCount),
    silentCount,
    abnormalCount,
    missingKinds: AUDIO_KINDS.filter((k) => Number(scanned[k] || 0) === 0),
    scanned,
    legend,
    failed,
    failedByKind,
    mode: String(audio.mode || ''),
  };
}

export interface KindState {
  key: 'ok' | 'silent' | 'bad' | 'missing';
  count: number;
  silent: number;
  bad: number;
}

export function kindStateOf(kind: string, s: AudioSummary): KindState {
  const count = Number(s.scanned[kind] || 0);
  const rows = s.failedByKind[kind] || [];
  const silent = rows.filter(isSilentFail).length;
  const bad = rows.length - silent;
  if (count === 0) return { key: 'missing', count, silent, bad };
  if (silent > 0) return { key: 'silent', count, silent, bad };
  if (bad > 0) return { key: 'bad', count, silent, bad };
  return { key: 'ok', count, silent, bad };
}

/**
 * 门禁处置：前端读不到 MJSCXT_FINAL_AUDIO_GATE，只能按**可观测事实**推断：
 *   · 结论不合格 + 成片未产出 + A 层 failed → 成片步骤被拦下 → blocked
 *   · 结论不合格 + 成片已产出（已登记交付） → 放行、只记录 → record_only
 *   · 结论合格（blocked=false 且已判定）    → untriggered
 *   · 没有结论/拿不到判据                   → unknown
 */
export type GateState = 'blocked' | 'record_only' | 'untriggered' | 'unknown';

export interface GateInput {
  audioQc?: { available?: boolean; blocked?: boolean } | null;
  /** 该集成片是否已产出（quality.artifact.exists） */
  artifactExists?: boolean;
  /** A 层（技术渲染）状态（quality.state.A.status） */
  stageAStatus?: string;
}

export function resolveGate(i: GateInput): GateState {
  const aq = i.audioQc;
  if (!aq || aq.available === false) return 'unknown';
  if (!aq.blocked) return 'untriggered';
  if (i.artifactExists === true) return 'record_only';
  if (i.artifactExists === false && String(i.stageAStatus || '') === 'failed') return 'blocked';
  return 'unknown';
}
