import React, { useCallback, useEffect, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { Badge, Button, Select } from '@/components/ui';
import DepsReportPanel from '@/components/DepsReportPanel';
import { AlertTriangle, RefreshCw } from '@/components/ui/icons';
import { comfyuiModelsApi } from '@/api/client';
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
