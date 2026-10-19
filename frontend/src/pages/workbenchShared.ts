// E-5（2026-10-16）：从 ProjectWorkbenchPage.tsx 抽出的**跨组件共享**定义。
// 被页面本体 + 多个抽出的组件（AssetCard / OverviewTab / StoryboardTab / ChatPanel / …）共用。
import { useState, useEffect } from 'react';
import { autopilotApi, episodesApi, type VideoMode } from '@/api/client';

export interface AssetItem {
  name: string;
  url?: string;
  file?: string;
  size?: number;
  /** 文件 mtime（后端下发的版本令牌）：拼进图片 URL 做缓存击穿，保证重生成后前端必刷新 */
  mtime?: number;
  [key: string]: any;
}

// ========== 资源地址解析 ==========
// 后端 /api/projects/<pid>/assets 返回的 url 已带 "/api" 前缀，不能重复拼接
export function assetSrc(url?: string | null, version?: number | string | null): string | null {
  if (!url) return null;
  let out = url;
  if (!(out.startsWith('http://') || out.startsWith('https://'))) {
    out = out.startsWith('/') ? out : `/api/${out}`;
  }
  // ⭐ 2026-10-05 资产自动刷新：带上版本令牌（后端 asset_gallery 下发的 mtime）。
  //   背景：资产重新生成后 URL 是不变的（/api/assets/<key>/<name>/base.png），
  //   轮询虽然换回了新 JSON，但 <img src> 字符串没变 —— React 不会重挂节点、
  //   浏览器还会命中旧缓存，用户看到的仍是旧图，误以为「必须手动刷新页面」。
  //   加上 ?v=<mtime> 后：图变了 URL 就变，React 必然重挂 <img>，浏览器必然重新取图。
  if (version !== undefined && version !== null && `${version}` !== '') {
    out += (out.includes('?') ? '&' : '?') + 'v=' + encodeURIComponent(String(version));
  }
  return out;
}

export interface ProjectAssets {
  success: boolean;
  project: string;
  gallery: Record<string, AssetItem[]>;
  storyboards: AssetItem[];
  videos: AssetItem[];
  final: AssetItem[];
  dub: AssetItem[];
  counts: Record<string, number>;
}

// 生产流水线步骤序列：与后端 pipeline.STEP_SEQUENCE 对齐（技术标识符 → i18n 键）。
export const PRODUCTION_STEPS: { id: string; labelKey: string }[] = [
  { id: 'script', labelKey: 'wb.stepScript' },
  { id: 'tts_pre', labelKey: 'wb.stepTtsPre' },
  { id: 'assets', labelKey: 'wb.stepAssets' },
  { id: 'storyboard', labelKey: 'wb.stepStoryboard' },
  { id: 'video', labelKey: 'wb.stepVideo' },
  { id: 'upscale', labelKey: 'wb.stepUpscale' },
  { id: 'final', labelKey: 'wb.stepFinal' },
];

/** 生产步骤标识符 → i18n 键（与工作台 PRODUCTION_STEPS / 后端 pipeline.STEP_SEQUENCE 同名）。
 *  未知子阶段（如 `script:outline`）取冒号前的环节；再取不到就回落原始标识符。 */
export const STEP_LABEL_KEY: Record<string, string> = {
  script: 'wb.stepScript',
  tts_pre: 'wb.stepTtsPre',
  assets: 'wb.stepAssets',
  storyboard: 'wb.stepStoryboard',
  video: 'wb.stepVideo',
  upscale: 'wb.stepUpscale',
  final: 'wb.stepFinal',
};

/** 状态轮询间隔：与 ProductionProgress 的 3s 同量级，足够反映「当前步骤」变化又不打扰后端 */
export const STATUS_POLL_MS = 4000;

export interface StartProductionCardProps {
  /** 当前项目 key（工作台路由 `?p=`）；空串 = 尚未选择项目 → 禁用并说明 */
  projectKey: string;
  /** 可生产的集号（升序）。空数组 = 还没有剧集 → 空态 + 说明「先生成剧本」 */
  episodes: number[];
  /** 剧集列表首次加载中（父组件 `scriptLoading`） */
  episodesLoading?: boolean;
  /** 剧集列表加载失败（父组件已脱敏的文案） */
  episodesError?: string;
  /** 空态/失败态「重新读取剧集列表」的动作（父组件 `fetchEpisodes`） */
  onRefreshEpisodes?: () => void;
}

export function useEpisodeList(novelId?: string) {
  const [episodes, setEpisodes] = useState<any[]>([]);
  useEffect(() => {
    if (!novelId) {
      setEpisodes([]);
      return;
    }
    let alive = true;
    episodesApi.list(novelId)
      .then((d) => { if (alive) setEpisodes(d.episodes || []); })
      .catch(() => { if (alive) setEpisodes([]); });
    return () => { alive = false; };
  }, [novelId]);
  return episodes;
}

// 项目级视频生成方式（写入 config.video_mode）；与后端 norm_video_mode 同源，只保留「整集一次生成」。
export const PROJECT_VIDEO_MODE_OPTIONS: { value: VideoMode; labelKey: string }[] = [
  { value: 'episode', labelKey: 'project.videoModeEpisode' },
];

// 批量重生成单次上限：与后端 /api/video/retry-shots-batch 的硬上限（≤12）一致
export const BATCH_RETRY_LIMIT = 12;

export function useProductionStatus(projectKey: string) {
  const [current, setCurrent] = useState<any>(null);
  useEffect(() => {
    if (!projectKey) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      let next = 3000;
      try {
        const st = await autopilotApi.status(projectKey);
        if (!alive) return;
        const cur = st?.current || null;
        setCurrent(cur);
        // 有活儿 → 3s 紧轮询；没活儿 → 12s 慢轮询（等新任务起来）
        next = cur ? 3000 : 12000;
      } catch {
        if (!alive) return;
        next = 12000;
      }
      if (alive) timer = setTimeout(tick, next);
    };
    tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [projectKey]);
  return current;
}
