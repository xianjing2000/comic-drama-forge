import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { Badge, Button, Select } from '@/components/ui';
import DepsReportPanel from '@/components/DepsReportPanel';
import { AlertTriangle, CheckCircle2, Mic, RefreshCw, ZoomIn } from '@/components/ui/icons';
import { comfyuiModelsApi, ttsApi, upscaleApi } from '@/api/client';
import type { ComfyUIModelsResponse } from '@/types';

/**
 * ComfyUI 生成模型（全局设置）
 *
 * 为什么不直接在工作流模板里写死模型文件名：ComfyUI 某个节点下拉框的**合法值**
 * 取决于模型文件放在哪个目录（放进 diffusion_models/minimax-h3/ 之后，报出的名字
 * 会带 `minimax-h3\` 前缀）。模板里一旦写的是裸文件名，与实际磁盘布局不符时，
 * 该节点会被校验失败 —— 而且往往是**静默丢弃产出**，不报显式错误，非常难查。
 *
 * 因此这里直接向 ComfyUI 查询权威候选值，由用户手动指定，并即时生效于后续提交。
 */
// ============================================================================
// 环境自检区块（2026-10-10 用户要求：把「配音环境自检」与「超分」两块检测
// 从各自所在页**移动**到本页（「生成模型」→「模型检测」），
// 让这一页成为**统一的「运行环境与模型可用性检测」入口**）。
//
// 数据源与原页完全一致（ttsApi.env / upscaleApi.env），口径不另立一份：
//   · /api/tts/env     → ComfyUI 在线 / 期望模型清单及命中 / 可用音色数 / 是否可配音
//   · /api/upscale/env → 超分可用 / 引擎（TE-Speed 加速 或 legacy FlashVSR）/
//                        ComfyUI 在线 / 模型就绪 / 节点备用
// ⚠️ 实测这两个接口各需 6~8 秒（要探测 ComfyUI 节点树），故各自独立加载、
//    独立转圈，互不阻塞本页的模型扫描。
// ============================================================================
function VoiceEnvCheck() {
  const [env, setEnv] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const load = React.useCallback(async () => {
    setLoading(true); setErr('');
    try { setEnv(await ttsApi.env()); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const models = env?.expected_models || [];
  const found = models.filter((m: any) => m.found).length;
  const speakers = env?.voices?.speakers || [];
  return (
    <div className='rounded-lg border border-line/60 bg-surface-2/40 p-4'>
      <div className='flex items-center justify-between gap-2'>
        <div className='flex items-center gap-2'>
          <Mic className='h-4 w-4 text-ink-3' />
          <span className='text-sm font-semibold text-ink-1'>配音环境自检</span>
        </div>
        <Button size='sm' variant='ghost' disabled={loading} onClick={() => void load()}>
          <RefreshCw className={loading ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
        </Button>
      </div>
      {err ? (
        <p className='mt-2 text-xs text-danger'>{err}</p>
      ) : loading && !env ? (
        <p className='mt-2 text-xs text-ink-3'>检测中…（需探测 ComfyUI 节点树，约 8 秒）</p>
      ) : (
        <>
          <div className='mt-2 flex flex-wrap items-center gap-2 text-xs'>
            <span className={env?.comfyui_online ? 'flex items-center gap-1.5 text-success' : 'flex items-center gap-1.5 text-warning'}>
              {env?.comfyui_online ? <CheckCircle2 className='h-3.5 w-3.5' /> : <AlertTriangle className='h-3.5 w-3.5' />}
              ComfyUI {env?.comfyui_online ? '在线' : '离线'}
            </span>
            <span className='text-ink-3'>模型 {found}/{models.length} 就绪</span>
            {speakers.length > 0 ? <Badge variant='info'>{speakers.length} 个音色</Badge> : null}
            {env?.available ? <Badge variant='success'>可配音</Badge> : <Badge variant='danger'>不可用</Badge>}
          </div>
          {models.length > 0 && (
            <div className='mt-2 flex flex-col gap-1'>
              {models.map((m: any, i: number) => (
                <div key={i} className='flex items-center gap-2 text-xs'>
                  <span className={m.found ? 'h-1.5 w-1.5 shrink-0 rounded-full bg-success' : 'h-1.5 w-1.5 shrink-0 rounded-full bg-danger'} />
                  <span className='w-56 shrink-0 truncate text-ink-2'>{m.name}</span>
                  <span className='min-w-0 flex-1 truncate text-ink-3'>{m.desc}</span>
                  {(m.matched || []).length > 0 ? <span className='shrink-0 text-ink-3'>命中 {m.matched.length}</span> : null}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function UpscaleEnvCheck() {
  const [env, setEnv] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const load = React.useCallback(async () => {
    setLoading(true); setErr('');
    try { setEnv(await upscaleApi.env()); }
    catch (e) { setErr(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const avail = !!env?.available;
  const eng = env?.default_engine === 'legacy-flashvsr' ? 'legacy FlashVSR' : 'TE-Speed（加速）';
  return (
    <div className='rounded-lg border border-line/60 bg-surface-2/40 p-4'>
      <div className='flex items-center justify-between gap-2'>
        <div className='flex items-center gap-2'>
          <ZoomIn className='h-4 w-4 text-ink-3' />
          <span className='text-sm font-semibold text-ink-1'>超分（FlashVSR）</span>
        </div>
        <Button size='sm' variant='ghost' disabled={loading} onClick={() => void load()}>
          <RefreshCw className={loading ? 'h-3.5 w-3.5 animate-spin' : 'h-3.5 w-3.5'} />
        </Button>
      </div>
      <p className='mt-1 text-xs text-ink-3'>把小分辨率成片放大为高清版本。逐帧处理、耗时较长，属于画质增强的可选环节。</p>
      {err ? (
        <p className='mt-2 text-xs text-danger'>{err}</p>
      ) : loading && !env ? (
        <p className='mt-2 text-xs text-ink-3'>检测中…</p>
      ) : (
        <>
          <div className={avail ? 'mt-2 rounded-md border border-success/40 bg-success/5 p-3 text-xs' : 'mt-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-xs'}>
            <div className='flex flex-wrap items-center justify-between gap-2'>
              <span className={avail ? 'font-medium text-success-strong' : 'font-medium text-warning-strong'}>
                {avail ? '超分链路就绪' : '超分链路不可用'}
              </span>
              {avail ? <span className='text-ink-3'>引擎: {eng}</span> : null}
            </div>
            <div className='mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-ink-2'>
              <span>ComfyUI 在线: {env?.comfy_online ? '✓' : '✗'}</span>
              <span>FlashVSR 模型: {env?.models_ok || env?.model_ok ? '✓' : '✗'}</span>
              <span>TE-Speed 加速链路: {env?.default_engine !== 'legacy-flashvsr' ? '✓' : '✗'}</span>
              <span>旧 FlashVSR 链路: {env?.alt_nodes ? '✓' : '✗'}</span>
            </div>
            <p className='mt-2 text-ink-3'>自动生产流程已默认开启超分：环境可用时自动执行，不可用时自动跳过，不会影响正常出片。</p>
          </div>
        </>
      )}
    </div>
  );
}

export function ComfyUIPage() {
  const { t } = useApp();
  const [data, setData] = useState<ComfyUIModelsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const scan = useCallback(async (refresh: boolean) => {
    setLoading(true);
    setError('');
    try {
      setData(await comfyuiModelsApi.scan(refresh));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void scan(false);
  }, [scan]);

  const offline = data ? !data.success : false;

  return (
    <div className="space-y-6 p-6">
      {/* ===== Header ===== */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">{t('wb.cm.title')}</h2>
          <p className="mt-1 max-w-3xl text-sm text-ink-3">{t('wb.cm.subtitle')}</p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          disabled={loading}
          onClick={() => scan(true)}
        >
          <RefreshCw className={`mr-2 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          {loading ? t('wb.cm.scanning') : t('wb.cm.scan')}
        </Button>
      </div>

      {/* ===== 依赖就绪：按当前生产工作流比对 ComfyUI 的模型与自定义节点 ===== */}
      <DepsReportPanel />

      {/* ===== 环境自检（2026-10-10 用户要求：从「配音」与「超分」两处移入本页）===== */}
      <div className="grid gap-4 lg:grid-cols-2">
        <VoiceEnvCheck />
        <UpscaleEnvCheck />
      </div>

      {/* ===== Stats ===== */}
      {data?.success && (
        <div className="flex flex-wrap items-center gap-2">
          {typeof data.node_type_count === 'number' && (
            <Badge variant="default">
              {t('wb.cm.nodeTypes')}: {data.node_type_count}
            </Badge>
          )}
          {typeof data.core_node_count === 'number' && (
            <Badge variant="default">
              {t('wb.cm.coreNodes')}: {data.core_node_count}
            </Badge>
          )}
          <Badge variant="default">
            {t('wb.cm.customPkgs')}: {data.plugins?.length ?? 0}
          </Badge>
          {data.scanned_at && (
            <span className="text-xs text-ink-3">
              {t('wb.cm.scannedAt')}: {data.scanned_at}
            </span>
          )}
        </div>
      )}

      {/* ===== Error / Offline ===== */}
      {(error || offline) && (
        <div className="flex items-start gap-3 rounded-lg border border-warning/40 bg-warning/5 p-4">
          <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-warning" />
          <div className="text-sm">
            <p className="font-medium text-warning-strong">
              {t('wb.cm.offline')}
            </p>
            <p className="mt-1 text-ink-3">{error || data?.error || t('wb.cm.offlineHint')}</p>
          </div>
        </div>
      )}

    </div>
  );
}
