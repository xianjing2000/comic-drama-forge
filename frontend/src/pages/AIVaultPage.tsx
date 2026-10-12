import React, { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { aiConfigApi, watermarkApi, promptEnhanceApi } from '@/api/client';
import { Badge, Button, Card, ConfirmDialog, Input, Select, Skeleton } from '@/components/ui';
import { AlertTriangle, Brain, CheckCircle2, ChevronDown, Eye, EyeOff, Pencil, RefreshCw, Search, X } from '@/components/ui/icons';
import type { IconProps } from '@/components/ui/icons';
import type { AIConfigModule, AIConfigResponse, AITestResult, AIFallbackModel, AIProviderModelsResponse } from '@/types';
import { PromptManagerTab } from '@/components/PromptManagerTab';
import { sanitizeErrorMessage } from '@/utils/errorText';

type ModuleKey = 'text' | 'qc';

interface ModuleState {
  base_url: string;
  model: string;
  api_key: string;
  has_api_key: boolean;
  /** 思考档位：'' = 不注入（服务端默认），其余为 low / high / max */
  reasoning_effort: string;
}

/** 提供商模型清单检测的三态状态（t3）。error 已经过 sanitizeErrorMessage 脱敏。 */
interface ProviderModelsState {
  loading: boolean;
  loaded: boolean;
  models: string[];
  error: string;
  models_url: string;
  api_key_masked: string;
}

const EMPTY_MODELS: ProviderModelsState = {
  loading: false, loaded: false, models: [], error: '', models_url: '', api_key_masked: '',
};

const EMPTY_MODULE: ModuleState = {
  base_url: '', model: '', api_key: '', has_api_key: false, reasoning_effort: '',
};

/** 思考档位下拉的兜底选项（后端会下发 reasoning_effort_options，拿不到时用这份） */
const FALLBACK_REASONING_OPTIONS = ['', 'off', 'low', 'high', 'max'];

/** 档位说明的 i18n 键（键为后端下发的原始值） */
const REASONING_EFFORT_LABEL_KEYS: Record<string, string> = {
  '': 'vault.reasoning.default',
  off: 'vault.reasoning.off',
  low: 'vault.reasoning.low',
  high: 'vault.reasoning.high',
  max: 'vault.reasoning.max',
};

interface SystemSettings {
  comfyui_url: string;
  watermark_enabled: boolean;
  watermark_text: string;
}

/**
* 模块卡片头部的图标底色 / 字色。
* 统一走语义 token，与「卡片白底 + 细描边」的观感一致。
* 合并后只保留「纯文本模型(text)」与「质检模型(qc)」两个模块（chat 已并入 text）。
*/
const MODULE_CONFIG: Record<ModuleKey, {
  icon: React.ComponentType<IconProps>;
  titleKey: string;
  descKey: string;
  placeholderUrl: string;
  placeholderModel: string;
  probe?: string;
  tone: string;
}> = {
  text: {
    icon: Brain,
    titleKey: 'vault.module.text.title',
    descKey: 'vault.module.text.desc',
    placeholderUrl: 'https://api.deepseek.com/v1',
    placeholderModel: 'deepseek-chat',
    tone: 'bg-brand-subtle text-brand',
  },
  qc: {
    icon: Search,
    titleKey: 'vault.module.qc.title',
    descKey: 'vault.module.qc.desc',
    placeholderUrl: 'https://api.openai.com/v1',
    placeholderModel: 'gpt-4o-mini',
    probe: 'vision',
    tone: 'bg-accent-subtle text-accent',
  },
};

/**
 * 模型选择器：手填兜底 + 模型清单下拉二合一（t3）。
 *
 * 输入框始终可自由输入（模型清单拉取失败 / 为空时手填兜底）；检测到模型清单后，
 * 聚焦 / 点击右侧箭头展开下拉，选中即回填。三态（loading / empty / error）由父组件
 * 在下方用 renderModelsStatus 渲染。
 */
function ModelPicker({ value, onChange, placeholder, models, disabled, label }: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  models: string[];
  disabled?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const hasOptions = models.length > 0;
  const showList = open && hasOptions;

  return (
    <div>
      {label && <span className="mb-1 block text-sm font-medium text-ink-2">{label}</span>}
      <div ref={boxRef} className="relative">
        <input
          type="text"
          value={value}
          onChange={e => onChange(e.target.value)}
          onFocus={() => { if (hasOptions) setOpen(true); }}
          placeholder={placeholder}
          disabled={disabled}
          className="w-full rounded-md border bg-surface px-3 text-base text-ink-1 transition-colors placeholder:text-ink-3 h-9 font-mono text-sm border-line hover:border-line-strong focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/25 disabled:cursor-not-allowed disabled:opacity-50 pr-8"
        />
        {hasOptions && (
          <button
            type="button"
            onClick={() => setOpen(o => !o)}
            disabled={disabled}
            tabIndex={-1}
            className="absolute inset-y-0 right-0 flex items-center pr-2 text-ink-3 hover:text-ink-1"
            aria-label="toggle model list"
          >
            <ChevronDown className={'h-4 w-4 transition-transform ' + (open ? 'rotate-180' : '')} />
          </button>
        )}
        {showList && (
          <ul className="absolute z-20 mt-1 max-h-56 w-full overflow-auto rounded-md border border-line bg-surface py-1 shadow-lg">
            {models.map(m => (
              <li key={m}>
                <button
                  type="button"
                  onClick={() => { onChange(m); setOpen(false); }}
                  className="block w-full px-3 py-1.5 text-left font-mono text-sm text-ink-1 hover:bg-surface-2"
                >
                  {m}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

export function AIVaultPage() {
  const { t } = useApp();
  // AI 模块配置状态
  const [config, setConfig] = useState<Record<ModuleKey, ModuleState>>({
    text: { ...EMPTY_MODULE },
    qc: { ...EMPTY_MODULE },
  });
  const [reOptions, setReOptions] = useState<string[]>(FALLBACK_REASONING_OPTIONS);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<Record<ModuleKey, AITestResult | null>>({
    text: null, qc: null,
  });
  // 备用模型（故障转移链）：按模块维护，主模型挂 3 次自动切下一个，任务不中断
  const [fallbacks, setFallbacks] = useState<Record<ModuleKey, AIFallbackModel[]>>({ text: [], qc: [] });
  const [showAddFb, setShowAddFb] = useState<ModuleKey | null>(null);
  const [fbDraft, setFbDraft] = useState<AIFallbackModel>({ base_url: '', model: '', api_key: '', label: '' });
  /** 正在编辑哪一条备用（null = 新增）；编辑时密钥回填脱敏值，提交即「不改动原密钥」 */
  const [editingFb, setEditingFb] = useState<{ module: ModuleKey; index: number } | null>(null);
  /** 每条备用各自的连通性测试结果，key = module + '#' + index */
  const [fbResults, setFbResults] = useState<Record<string, AITestResult | null>>({});
  const [fbTesting, setFbTesting] = useState<string | null>(null);
  /** 备用列表「已改但未保存」标记 */
  const [fbDirty, setFbDirty] = useState<Record<ModuleKey, boolean>>({ text: false, qc: false });
  /** 备用列表正在落盘（添加/编辑/删除/排序后自动提交，不需要用户再点一次「保存配置」） */
  const [fbSaving, setFbSaving] = useState<ModuleKey | null>(null);
  /** 主模型各模块的「提供商模型清单检测」状态（t3） */
  const [modelsState, setModelsState] = useState<Record<ModuleKey, ProviderModelsState>>({
    text: { ...EMPTY_MODELS }, qc: { ...EMPTY_MODELS },
  });
  /** 备用草稿表单的「提供商模型清单检测」状态（t3） */
  const [fbModelsState, setFbModelsState] = useState<ProviderModelsState>({ ...EMPTY_MODELS });
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [clearTarget, setClearTarget] = useState<ModuleKey | null>(null);
  const [clearing, setClearing] = useState(false);
  /** API Key 明文可见性（按模块记忆，眼睛按钮切换） */
  const [visibleKeys, setVisibleKeys] = useState<Record<ModuleKey, boolean>>({ text: false, qc: false });
  /** 正在拉取已保存密钥明文的模块（回填期间按钮禁用） */
  const [revealing, setRevealing] = useState<ModuleKey | null>(null);

  // 系统设置状态（ComfyUI 只读展示 + 水印）
  const [sysSettings, setSysSettings] = useState<SystemSettings>({
    comfyui_url: '',
    watermark_enabled: false,
    watermark_text: '',
  });
  const [sysSaving, setSysSaving] = useState(false);

  // 提示词增强开关（2026-09-30）：出图/出片前 LLM 增强 + 质检模型复审。
  // 保存后立即生效（后端每次生成都重读开关文件），无需重启。
  const [pe, setPe] = useState<{ enhance: boolean; review: boolean; envOverridden: boolean }>({ enhance: true, review: true, envOverridden: false });
  const [peSaving, setPeSaving] = useState(false);

  useEffect(() => {
    Promise.all([
      // 加载 AI 模块配置
      aiConfigApi.get()
        .then(d => {
          // 后端实际结构：config = { modules: {text,qc}, modules_meta, ... }
          const raw = (d.config || {}) as any;
          // t2 合并后 modules 只剩 text/qc；此处用 Record<string,...> 兼容老数据里可能残留的 chat
          const c = (raw.modules || raw) as Record<string, AIConfigModule>;
          if (raw.comfyui?.url) {
            setSysSettings(prev => ({ ...prev, comfyui_url: String(raw.comfyui.url) }));
          }
          if (Array.isArray(raw.reasoning_effort_options) && raw.reasoning_effort_options.length) {
            setReOptions(raw.reasoning_effort_options.map((x: unknown) => String(x)));
          }
          // text 优先：老数据 text 空而 chat 有值时取 chat（与后端合并策略一致，纯兜底）
          const textCfg = (c.text && (c.text.base_url || c.text.model)) ? c.text : c.chat;
          const qcCfg = c.qc;
          // 回填备用模型（故障转移链）
          setFallbacks({
            text: Array.isArray(textCfg?.fallbacks) ? textCfg!.fallbacks!.map(f => ({ ...f })) : [],
            qc: Array.isArray(qcCfg?.fallbacks) ? qcCfg!.fallbacks!.map(f => ({ ...f })) : [],
          });
          setFbDirty({ text: false, qc: false });
          setConfig({
            text: {
              base_url: textCfg?.base_url || '',
              model: textCfg?.model || '',
              api_key: textCfg?.api_key || '',
              has_api_key: textCfg?.has_api_key || false,
              reasoning_effort: textCfg?.reasoning_effort || '',
            },
            qc: {
              base_url: qcCfg?.base_url || '',
              model: qcCfg?.model || '',
              api_key: qcCfg?.api_key || '',
              has_api_key: qcCfg?.has_api_key || false,
              reasoning_effort: qcCfg?.reasoning_effort || '',
            },
          });
        })
        .catch(err => console.error('Failed to load AI config:', err)),
      // 加载水印设置：走专用的 /watermark/config。
      watermarkApi.get()
        .then(d => {
          const w = d.config || {};
          setSysSettings(prev => ({
            ...prev,
            watermark_enabled: Boolean(w.enabled),
            watermark_text: String(w.text || ''),
          }));
        })
        .catch(() => {}),
      // 加载提示词增强开关
      promptEnhanceApi.get()
        .then(d => {
          const eff = d.effective || {};
          setPe({
            enhance: eff.enhance_enabled !== false,
            review: eff.review_enabled !== false,
            envOverridden: Boolean((d.env_overridden || {}).enhance || (d.env_overridden || {}).review),
          });
        })
        .catch(() => {}),
    ]).finally(() => setLoading(false));
  }, []);

  const showMessage = (type: 'success' | 'error', text: string) => {
    setMessage({ type, text });
    setTimeout(() => setMessage(null), 4000);
  };

  /** 模型清单三态提示（主模型与备用表单共用） */
  const renderModelsStatus = (ms: ProviderModelsState) => {
    if (ms.loading) {
      return (
        <p className="mt-1 flex items-center gap-1.5 text-xs text-ink-3">
          <RefreshCw className="h-3 w-3 animate-spin" />
          {t('vault.detectingModels')}
        </p>
      );
    }
    if (ms.error) {
      return <p className="mt-1 text-xs text-danger-strong">{t('vault.modelsError', { err: ms.error })}</p>;
    }
    if (ms.loaded && ms.models.length === 0) {
      return <p className="mt-1 text-xs text-warning-strong">{t('vault.modelsEmpty')}</p>;
    }
    if (ms.loaded && ms.models.length > 0) {
      return <p className="mt-1 text-xs text-success-strong">{t('vault.modelsLoaded', { n: ms.models.length })}</p>;
    }
    return null;
  };

  /** 检测主模型的提供商模型清单（base_url + api_key → POST /api/ai/models） */
  const handleDetectModels = async (module: ModuleKey) => {
    const state = config[module];
    const baseUrl = state.base_url.trim();
    const apiKey = state.api_key.trim();
    // 已保存密钥且未回显明文 → 只传 module，后端复用保存的 base_url + 解密 key（明文不下发前端）
    const reuseSaved = state.has_api_key && !apiKey;
    if (!reuseSaved && (!baseUrl || !apiKey)) {
      showMessage('error', t('vault.detectNeedKey'));
      return;
    }
    setModelsState(prev => ({ ...prev, [module]: { ...EMPTY_MODELS, loading: true } }));
    try {
      const res = await aiConfigApi.detectModels(
        reuseSaved ? { module } : { base_url: baseUrl, api_key: apiKey }
      );
      if (res.success && Array.isArray(res.models)) {
        setModelsState(prev => ({ ...prev, [module]: {
          loading: false, loaded: true, models: res.models || [],
          error: '', models_url: res.models_url || '', api_key_masked: res.api_key_masked || '',
        }}));
        showMessage('success', t('vault.modelsLoaded', { n: (res.models || []).length }));
      } else {
        const err = sanitizeErrorMessage(String(res.error || res.hint || t('error.unknown')));
        setModelsState(prev => ({ ...prev, [module]: {
          loading: false, loaded: false, models: [],
          error: err, models_url: res.models_url || '', api_key_masked: res.api_key_masked || '',
        }}));
        showMessage('error', t('vault.modelsError', { err }));
      }
    } catch (err) {
      const msg = sanitizeErrorMessage(err instanceof Error ? err.message : t('error.unknown'));
      setModelsState(prev => ({ ...prev, [module]: {
        loading: false, loaded: false, models: [], error: msg, models_url: '', api_key_masked: '',
      }}));
      showMessage('error', t('vault.modelsError', { err: msg }));
    }
  };

  /** 检测备用草稿的提供商模型清单（显式 base_url + api_key） */
  const handleDetectFbModels = async () => {
    const baseUrl = (fbDraft.base_url || '').trim();
    const apiKey = (fbDraft.api_key || '').trim();
    if (!baseUrl || !apiKey) {
      showMessage('error', t('vault.detectNeedKey'));
      return;
    }
    setFbModelsState({ ...EMPTY_MODELS, loading: true });
    try {
      const res = await aiConfigApi.detectModels({ base_url: baseUrl, api_key: apiKey });
      if (res.success && Array.isArray(res.models)) {
        setFbModelsState({ loading: false, loaded: true, models: res.models || [], error: '', models_url: res.models_url || '', api_key_masked: res.api_key_masked || '' });
        showMessage('success', t('vault.modelsLoaded', { n: (res.models || []).length }));
      } else {
        const err = sanitizeErrorMessage(String(res.error || res.hint || t('error.unknown')));
        setFbModelsState({ loading: false, loaded: false, models: [], error: err, models_url: res.models_url || '', api_key_masked: res.api_key_masked || '' });
        showMessage('error', t('vault.modelsError', { err }));
      }
    } catch (err) {
      const msg = sanitizeErrorMessage(err instanceof Error ? err.message : t('error.unknown'));
      setFbModelsState({ loading: false, loaded: false, models: [], error: msg, models_url: '', api_key_masked: '' });
      showMessage('error', t('vault.modelsError', { err: msg }));
    }
  };

  /** 保存后回读：把服务端归一化后的 index / has_api_key / api_key_masked 拉回来 */
  const refreshFallbacks = async (module: ModuleKey) => {
    try {
      const d = await aiConfigApi.get();
      const raw2 = (d.config || {}) as any;
      const c2 = (raw2.modules || raw2) as Record<string, AIConfigModule>;
      setFallbacks(prev => ({
        ...prev,
        [module]: Array.isArray(c2[module]?.fallbacks) ? c2[module]!.fallbacks!.map(f => ({ ...f })) : [],
      }));
    } catch { /* 回读失败不影响已保存的结果 */ }
  };

  /** 把备用列表立刻落盘（添加/编辑/删除/排序后自动提交，两段式变一步） */
  const persistFallbacks = async (module: ModuleKey, list: AIFallbackModel[], okText?: string) => {
    const state = config[module];
    if (!state.base_url.trim() || !state.model.trim()) {
      setFbDirty(prev => ({ ...prev, [module]: true }));
      showMessage('error', t('vault.fallbackNeedMainFirst'));
      return;
    }
    // 只提交填了 base_url + model 的条目；密钥留空 = 不改动原密钥（后端按签名续上）
    const _fbs = list.map(f => ({
      base_url: f.base_url?.trim(),
      model: f.model?.trim(),
      api_key: f.api_key?.trim() || undefined,
      label: f.label?.trim() || undefined,
      reasoning_effort: f.reasoning_effort,
    })).filter(f => f.base_url && f.model);
    setFbSaving(module);
    try {
      const result = await aiConfigApi.save(
        module, state.base_url.trim(), state.model.trim(),
        state.api_key.trim() || undefined, state.reasoning_effort, _fbs);
      if (result.success) {
        setFbDirty(prev => ({ ...prev, [module]: false }));
        await refreshFallbacks(module);
        showMessage('success', okText || t('vault.fallbackSaved'));
      } else {
        setFbDirty(prev => ({ ...prev, [module]: true }));
        showMessage('error', t('settings.saveFailed'));
      }
    } catch (err) {
      setFbDirty(prev => ({ ...prev, [module]: true }));
      showMessage('error', t('vault.saveFailedDetail', { err: err instanceof Error ? err.message : t('error.unknown') }));
    } finally {
      setFbSaving(null);
    }
  };

  // ========== AI 模块操作 ==========
  const handleSave = async (module: ModuleKey) => {
    const state = config[module];
    if (!state.base_url.trim() || !state.model.trim()) {
      showMessage('error', t('vault.err.requireUrlModel', { module: t(MODULE_CONFIG[module].titleKey) }));
      return;
    }
    if (!state.has_api_key && !state.api_key.trim()) {
      showMessage('error', t('vault.err.requireApiKey', { module: t(MODULE_CONFIG[module].titleKey) }));
      return;
    }

    setSaving(module);
    try {
      const _fbs = (fallbacks[module] || []).map(f => ({
        base_url: f.base_url?.trim(),
        model: f.model?.trim(),
        api_key: f.api_key?.trim() || undefined,
        label: f.label?.trim() || undefined,
        reasoning_effort: f.reasoning_effort,
      })).filter(f => f.base_url && f.model);
      const result = await aiConfigApi.save(
        module,
        state.base_url.trim(),
        state.model.trim(),
        state.api_key.trim() || undefined,
        state.reasoning_effort,
        _fbs
      );
      if (result.success) {
        setFbDirty(prev => ({ ...prev, [module]: false }));
        await refreshFallbacks(module);
        showMessage('success', result.message || t('vault.msg.saved', { module: t(MODULE_CONFIG[module].titleKey) }));
        setConfig(prev => ({
          ...prev,
          [module]: {
            ...prev[module],
            has_api_key: result.module_config?.has_api_key ?? prev[module].has_api_key,
            reasoning_effort: result.module_config?.reasoning_effort ?? prev[module].reasoning_effort,
          },
        }));
      } else {
        showMessage('error', t('settings.saveFailed'));
      }
    } catch (err) {
      showMessage('error', t('vault.saveFailedDetail', { err: err instanceof Error ? err.message : t('error.unknown') }));
    } finally {
      setSaving(null);
    }
  };

  const handleTest = async (module: ModuleKey) => {
    const state = config[module];
    if (!state.base_url.trim() || !state.model.trim()) {
      showMessage('error', t('vault.err.requireUrlModelFirst', { module: t(MODULE_CONFIG[module].titleKey) }));
      return;
    }

    setTesting(module);
    setTestResult(prev => ({ ...prev, [module]: null }));
    try {
      const probe = MODULE_CONFIG[module].probe || 'text';
      const result = await aiConfigApi.test(
        module,
        state.base_url.trim(),
        state.model.trim(),
        state.api_key.trim() || undefined,
        probe,
        30,
        state.reasoning_effort
      );
      setTestResult(prev => ({ ...prev, [module]: result }));
      if (result.success) {
        const ms = result.latency_ms ?? result.response_time_ms;
        const partial = result.verdict !== 'ok';
        showMessage(
          partial ? 'error' : 'success',
          partial
            ? t('vault.msg.partialReachable', { module: t(MODULE_CONFIG[module].titleKey) })
            : t('vault.msg.testOk', {
                module: t(MODULE_CONFIG[module].titleKey),
                ms: typeof ms === 'number' ? ' (' + ms + 'ms)' : '',
              })
        );
      } else {
        showMessage('error', result.error || result.guide || t('vault.connTestFailed'));
      }
    } catch (err) {
      setTestResult(prev => ({
        ...prev,
        [module]: { success: false, module, probe: 'text', error: err instanceof Error ? err.message : t('error.unknown') }
      }));
      showMessage('error', t('vault.testFailedDetail', { err: err instanceof Error ? err.message : t('error.unknown') }));
    } finally {
      setTesting(null);
    }
  };

  /** 打开某条备用的编辑表单（密钥回填脱敏值 → 不改动即保留原密钥） */
  const openFbEditor = (module: ModuleKey, index: number) => {
    const fb = (fallbacks[module] || [])[index];
    if (!fb) return;
    setFbDraft({
      base_url: fb.base_url || '',
      model: fb.model || '',
      api_key: fb.api_key_masked || fb.api_key || '',
      label: fb.label || '',
      reasoning_effort: fb.reasoning_effort || '',
    });
    setEditingFb({ module, index });
    setShowAddFb(module);
    setFbModelsState({ ...EMPTY_MODELS });
  };

  /** 关闭新增/编辑表单 */
  const closeFbForm = () => {
    setShowAddFb(null);
    setEditingFb(null);
    setFbDraft({ base_url: '', model: '', api_key: '', label: '', reasoning_effort: '' });
    setFbModelsState({ ...EMPTY_MODELS });
  };

  /** 测试某条备用（已保存无明文 → 按索引由后端取解密 key；草稿 → 显式 base_url/model/key） */
  const handleTestFallback = async (module: ModuleKey, index: number) => {
    const key = module + '#' + index;
    const fb = (fallbacks[module] || [])[index];
    const label = fb?.label || t('vault.fallbackN', { n: index + 1 });
    const typedKey = (fb?.api_key || '').trim();
    const canTestByIndex = !typedKey && typeof fb?.index === 'number';
    setFbTesting(key);
    setFbResults(prev => ({ ...prev, [key]: null }));
    try {
      const result = canTestByIndex
        ? await aiConfigApi.testFallback(module, fb!.index!, MODULE_CONFIG[module].probe || 'text', 30)
        : await aiConfigApi.test(module, fb?.base_url || '', fb?.model || '',
            typedKey || undefined, MODULE_CONFIG[module].probe || 'text', 30, fb?.reasoning_effort);
      setFbResults(prev => ({ ...prev, [key]: result }));
      const partial = result.success && result.verdict !== 'ok';
      showMessage(
        result.success && !partial ? 'success' : 'error',
        result.success
          ? (partial ? t('vault.fallbackTestPartial') : t('vault.fallbackTestOk', { label }))
          : (t('vault.fallbackTestFail') + '：' + (result.error || result.guide || '')).slice(0, 200)
      );
    } catch (err) {
      const msg = err instanceof Error ? err.message : t('error.unknown');
      setFbResults(prev => ({
        ...prev,
        [key]: { success: false, module, probe: 'text', error: msg } as AITestResult,
      }));
      showMessage('error', t('vault.fallbackTestFail') + '：' + msg);
    } finally {
      setFbTesting(null);
    }
  };

  /** 备用故障转移链排序：上移（-1）/下移（+1），改完立刻落盘 */
  const moveFallback = async (module: ModuleKey, index: number, dir: -1 | 1) => {
    const list = [...(fallbacks[module] || [])];
    const j = index + dir;
    if (j < 0 || j >= list.length) return;
    [list[index], list[j]] = [list[j], list[index]];
    setFallbacks(prev => ({ ...prev, [module]: list }));
    setFbResults(prev => {
      const next = { ...prev };
      Object.keys(next).forEach(k => { if (k.startsWith(module + '#')) delete next[k]; });
      return next;
    });
    persistFallbacks(module, list, t('vault.fallbackReordered'));
  };

  // 清空配置：统一确认弹窗
  const handleClear = (module: ModuleKey) => setClearTarget(module);

  // 眼睛按钮：切换明文/圆点。已保存的密钥后端默认不下发明文，首次点开时按需拉一次明文。
  const handleToggleVisible = async (moduleKey: ModuleKey) => {
    const turningOn = !visibleKeys[moduleKey];
    setVisibleKeys(prev => ({ ...prev, [moduleKey]: !prev[moduleKey] }));
    if (turningOn && !config[moduleKey].api_key.trim() && config[moduleKey].has_api_key && revealing === null) {
      setRevealing(moduleKey);
      try {
        const r = await aiConfigApi.revealKey(moduleKey);
        if (r?.success && r.api_key) {
          updateField(moduleKey, 'api_key', r.api_key);
        }
      } catch {
        showMessage('error', t('vault.revealFailed'));
      } finally {
        setRevealing(null);
      }
    }
  };

  const doClear = async () => {
    const module = clearTarget;
    if (!module) return;
    setClearing(true);
    try {
      const result = await aiConfigApi.clear(module);
      if (result.success) {
        setConfig(prev => ({
          ...prev,
          [module]: { ...EMPTY_MODULE },
        }));
        setModelsState(prev => ({ ...prev, [module]: { ...EMPTY_MODELS } }));
        showMessage('success', result.message);
      }
    } catch (err) {
      showMessage('error', t('vault.clearFailed'));
    } finally {
      setClearing(false);
      setClearTarget(null);
    }
  };

  const updateField = (module: ModuleKey, field: keyof ModuleState, value: string) => {
    setConfig(prev => ({
      ...prev,
      [module]: { ...prev[module], [field]: value },
    }));
  };

  // ========== 水印设置操作 ==========
  const handleSysSave = async () => {
    setSysSaving(true);
    try {
      await watermarkApi.update({
        enabled: sysSettings.watermark_enabled,
        text: sysSettings.watermark_text,
      });
      showMessage('success', t('vault.watermarkSaved'));
    } catch (err) {
      showMessage('error', t('vault.saveFailedDetail', { err: err instanceof Error ? err.message : t('error.unknown') }));
    } finally {
      setSysSaving(false);
    }
  };

  // ========== 提示词增强开关（保存后立即生效，无需重启） ==========
  const handlePeSave = async () => {
    setPeSaving(true);
    try {
      const d = await promptEnhanceApi.update({
        enhance_enabled: pe.enhance,
        review_enabled: pe.review,
      });
      const eff = d.effective || {};
      setPe(prev => ({
        ...prev,
        enhance: eff.enhance_enabled !== false,
        review: eff.review_enabled !== false,
      }));
      showMessage('success', t('vault.promptEnhanceSaved'));
    } catch (err) {
      showMessage('error', t('vault.saveFailedDetail', { err: err instanceof Error ? err.message : t('error.unknown') }));
    } finally {
      setPeSaving(false);
    }
  };

  const updateSysField = (field: keyof SystemSettings, value: string | boolean) => {
    setSysSettings(prev => ({ ...prev, [field]: value }));
  };

  if (loading) {
    return (
      <div className="space-y-6 max-w-5xl mx-auto" role="status" aria-live="polite" aria-label={t('vault.loadingConfig')}>
        <div className="space-y-2">
          <Skeleton className="h-7 w-40" />
          <Skeleton className="h-4 w-64" />
        </div>
        <Skeleton className="h-44 rounded-lg" />
        <div className="grid grid-cols-1 gap-6">
          {[0, 1].map((i) => (
            <Skeleton key={i} className="h-72 rounded-lg" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">{t('vault.title')}</h2>
          <p className="text-sm text-ink-2 mt-1">
            {t('vault.subtitle')}
          </p>
        </div>
        {message && (
          <div className={'px-4 py-2 rounded-lg text-sm ' + (
            message.type === 'success'
              ? 'bg-success-subtle text-success-strong'
              : 'bg-danger-subtle text-danger-strong'
          )}>
            {message.text}
          </div>
        )}
      </div>

      {/* ===== 系统设置区 ===== */}
      <Card bodyClassName="p-6 space-y-4">
        <h3 className="text-lg font-semibold text-ink-1 flex items-center gap-2">
          {t('vault.systemSettings')}
        </h3>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="md:col-span-2">
            <Input
              label={t('settings.comfyui.url')}
              value={sysSettings.comfyui_url || t('vault.comfyuiNotFetched')}
              onChange={() => undefined}
              disabled
              className="font-mono text-sm"
            />
            <p className="text-xs text-ink-2 mt-1">
              {t('vault.comfyuiEnvLead')}<code className="font-mono">COMFYUI_URL</code>{t('vault.comfyuiEnvTail')}
            </p>
          </div>
        </div>

        {/* 水印设置 */}
        <div className="border-t border-line pt-4 mt-4">
          <div className="flex items-center gap-3 mb-3">
            <input
              type="checkbox"
              checked={sysSettings.watermark_enabled}
              onChange={e => updateSysField('watermark_enabled', e.target.checked)}
              className="w-5 h-5 rounded border-line-strong text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
            />
            <span className="text-ink-2 font-medium">{t('vault.watermarkEnabled')}</span>
          </div>
          {sysSettings.watermark_enabled && (
            <div>
              <Input
                label={t('settings.watermark.text')}
                value={sysSettings.watermark_text}
                onChange={v => updateSysField('watermark_text', v)}
                placeholder={t('vault.watermarkPlaceholder')}
                className="text-sm"
              />
            </div>
          )}
        </div>

        {/* 提示词增强（2026-09-30） */}
        <div className="border-t border-line pt-4 mt-4">
          <div className="mb-2">
            <span className="text-ink-1 font-medium">{t('vault.promptEnhanceTitle')}</span>
            <p className="text-xs text-ink-3 mt-1">{t('vault.promptEnhanceDesc')}</p>
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={pe.enhance}
                onChange={e => setPe(p => ({ ...p, enhance: e.target.checked }))}
                className="w-5 h-5 rounded border-line-strong text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
              />
              <span className="text-ink-2 font-medium">{t('vault.promptEnhanceOn')}</span>
            </div>
            <div className="flex items-center gap-3">
              <input
                type="checkbox"
                checked={pe.review}
                onChange={e => setPe(p => ({ ...p, review: e.target.checked }))}
                className="w-5 h-5 rounded border-line-strong text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
              />
              <span className="text-ink-2 font-medium">{t('vault.promptReviewOn')}</span>
            </div>
            {pe.envOverridden && (
              <p className="text-xs text-warning-strong">{t('vault.promptEnhanceEnvNote')}</p>
            )}
          </div>
        </div>

        <div className="flex justify-end pt-2">
          <div className="flex items-center gap-3">
            <Button
              variant="secondary"
              onClick={handlePeSave}
              loading={peSaving}
            >
              {t('vault.savePromptEnhance')}
            </Button>
            <Button
              variant="brand"
              onClick={handleSysSave}
              loading={sysSaving}
            >
              {t('vault.saveWatermark')}
            </Button>
          </div>
        </div>
      </Card>

      {/* ===== AI 模块配置区（合并后只有 text / qc 两个模块） ===== */}
      <div className="grid grid-cols-1 gap-6">
        {(Object.keys(MODULE_CONFIG) as ModuleKey[]).map(moduleKey => {
          const meta = MODULE_CONFIG[moduleKey];
          const state = config[moduleKey];
          const isConfigured = state.base_url && state.model && state.has_api_key;
          const result = testResult[moduleKey];

          return (
            <Card key={moduleKey} bodyClassName="p-6 space-y-4">
              {/* Card Header */}
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-3">
                  <div className={'w-10 h-10 rounded-xl flex items-center justify-center ' + meta.tone}>
                    <meta.icon className="h-5 w-5" />
                  </div>
                  <div>
                    <h3 className="text-lg font-semibold text-ink-1">
                      {t(meta.titleKey)}
                    </h3>
                    <p className="text-xs text-ink-2">{t(meta.descKey)}</p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant={isConfigured ? 'success' : 'default'}>
                    {isConfigured ? t('vault.configured') : t('vault.notConfigured')}
                  </Badge>
                  <button
                    type="button"
                    onClick={() => handleClear(moduleKey)}
                    className="rounded-md p-2 text-ink-3 transition-colors hover:bg-surface-2 hover:text-danger focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
                    title={t('vault.clearConfig')}
                    aria-label={t('vault.clearConfig')}
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>

              {/* Config Fields */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="md:col-span-2">
                  <Input
                    label="Base URL"
                    value={state.base_url}
                    onChange={v => updateField(moduleKey, 'base_url', v)}
                    placeholder={meta.placeholderUrl}
                    className="font-mono text-sm"
                  />
                </div>
                <div>
                  <Input
                    type={visibleKeys[moduleKey] ? 'text' : 'password'}
                    label="API Key"
                    value={state.api_key}
                    onChange={v => updateField(moduleKey, 'api_key', v)}
                    placeholder={state.has_api_key ? '••••••••' : 'sk-...'}
                    className="font-mono text-sm"
                    suffix={
                      <button
                        type="button"
                        onClick={() => handleToggleVisible(moduleKey)}
                        disabled={revealing === moduleKey}
                        className="rounded-sm p-1.5 text-ink-3 transition-colors hover:text-ink-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 disabled:cursor-not-allowed disabled:opacity-50"
                        title={t(visibleKeys[moduleKey] ? 'vault.hideApiKey' : 'vault.showApiKey')}
                        aria-label={t(visibleKeys[moduleKey] ? 'vault.hideApiKey' : 'vault.showApiKey')}
                        aria-pressed={!!visibleKeys[moduleKey]}
                      >
                        {visibleKeys[moduleKey] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </button>
                    }
                  />
                  {state.has_api_key && (
                    <p className="text-xs text-ink-3 mt-1">{t('vault.keySavedHint')}</p>
                  )}
                </div>
                <div className="flex items-end">
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => handleDetectModels(moduleKey)}
                    loading={modelsState[moduleKey].loading}
                    title={t('vault.detectModels')}
                  >
                    {t('vault.detectModels')}
                  </Button>
                </div>
                {/* 模型：手填兜底 + 模型清单下拉（t3） */}
                <div className="md:col-span-2">
                  <ModelPicker
                    label="Model"
                    value={state.model}
                    onChange={v => updateField(moduleKey, 'model', v)}
                    placeholder={meta.placeholderModel}
                    models={modelsState[moduleKey].models}
                  />
                  {renderModelsStatus(modelsState[moduleKey])}
                  <p className="mt-1 text-xs text-ink-3">{t('vault.modelManualHint')}</p>
                </div>
                {/* 思考档位：只对「思考不可关闭」的模型（如 GLM-5.3-Flash）有意义。 */}
                <div className="md:col-span-2">
                  <Select
                    label={t('vault.reasoningLabel')}
                    value={state.reasoning_effort}
                    onChange={v => updateField(moduleKey, 'reasoning_effort', v)}
                    options={reOptions.map(opt => {
                      const labelKey = REASONING_EFFORT_LABEL_KEYS[opt];
                      return { value: opt, label: labelKey ? t(labelKey) : opt };
                    })}
                  />
                  <p className="text-xs text-ink-3 mt-1">
                    {t('vault.reasoningHintLead')}
                    <span className="text-warning-strong">
                      {t('vault.reasoningHintWarn')}
                    </span>
                    {t('vault.reasoningHintTail')}
                  </p>
                </div>
              </div>

              {/* ===== 备用模型（故障转移链） ===== */}
              <div className="border-t border-line pt-4 mt-2">
                <div className="flex items-center justify-between mb-2">
                  <div>
                    <span className="text-sm font-medium text-ink-1">{t('vault.fallbackTitle')}</span>
                    <p className="text-xs text-ink-3 mt-0.5">{t('vault.fallbackDesc')}</p>
                  </div>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setEditingFb(null);
                      setFbDraft({ base_url: '', model: '', api_key: '', label: '', reasoning_effort: '' });
                      setFbModelsState({ ...EMPTY_MODELS });
                      setShowAddFb(moduleKey);
                    }}
                  >
                    + {t('vault.fallbackAdd')}
                  </Button>
                </div>
                {fallbacks[moduleKey]?.length > 0 && (
                  <ul className="space-y-2">
                    {fallbacks[moduleKey].map((fb, i) => (
                      <li key={i} className="p-2 rounded-lg bg-surface-2 text-sm">
                        <div className="flex items-center gap-2">
                          {/* 排序：上移 / 下移（故障转移顺序） */}
                          <div className="flex items-center gap-0.5 shrink-0">
                            <button
                              type="button"
                              onClick={() => moveFallback(moduleKey, i, -1)}
                              disabled={i === 0 || fbSaving === moduleKey}
                              className="p-1 rounded text-ink-3 hover:text-brand transition-colors disabled:cursor-not-allowed disabled:opacity-30"
                              title={t('vault.fallbackMoveUp')}
                              aria-label={t('vault.fallbackMoveUp')}
                            >
                              <ChevronDown className="h-3.5 w-3.5 rotate-180" />
                            </button>
                            <button
                              type="button"
                              onClick={() => moveFallback(moduleKey, i, 1)}
                              disabled={i === fallbacks[moduleKey].length - 1 || fbSaving === moduleKey}
                              className="p-1 rounded text-ink-3 hover:text-brand transition-colors disabled:cursor-not-allowed disabled:opacity-30"
                              title={t('vault.fallbackMoveDown')}
                              aria-label={t('vault.fallbackMoveDown')}
                            >
                              <ChevronDown className="h-3.5 w-3.5" />
                            </button>
                          </div>
                          <div className="flex-1 min-w-0">
                            <div className="font-mono text-xs text-ink-1 truncate">
                              {fb.label || t('vault.fallbackN', { n: i + 1 })}
                              <span className="ml-2 text-ink-3">{fb.model}</span>
                            </div>
                            <div className="font-mono text-xs text-ink-3 truncate">{fb.base_url}</div>
                          </div>
                          {!(fb.has_api_key || fb.api_key_masked || (fb.api_key && fb.api_key.trim())) && (
                            <span className="text-xs text-warning-strong shrink-0">{t('vault.fallbackNoKey')}</span>
                          )}
                          <button
                            type="button"
                            onClick={() => handleTestFallback(moduleKey, i)}
                            disabled={fbTesting === moduleKey + '#' + i}
                            className="shrink-0 flex items-center gap-1 px-2 py-1 rounded border border-line text-xs text-ink-2 hover:text-brand hover:border-brand transition-colors disabled:opacity-40"
                            title={t('vault.fallbackTest')}
                          >
                            <RefreshCw className={'h-3 w-3 ' + (fbTesting === moduleKey + '#' + i ? 'animate-spin' : '')} />
                            {t('vault.fallbackTest')}
                          </button>
                          <button
                            type="button"
                            onClick={() => openFbEditor(moduleKey, i)}
                            className="shrink-0 flex items-center gap-1 px-2 py-1 rounded border border-line text-xs text-ink-2 hover:text-brand hover:border-brand transition-colors"
                            title={t('vault.fallbackEdit')}
                          >
                            <Pencil className="h-3 w-3" />
                            {t('vault.fallbackEdit')}
                          </button>
                          <button
                            type="button"
                            disabled={fbSaving === moduleKey}
                            onClick={() => {
                              const next = (fallbacks[moduleKey] || []).filter((_, idx) => idx !== i);
                              setFallbacks(prev => ({ ...prev, [moduleKey]: next }));
                              setFbResults(prev => {
                                const n = { ...prev };
                                Object.keys(n).forEach(k => { if (k.startsWith(moduleKey + '#')) delete n[k]; });
                                return n;
                              });
                              persistFallbacks(moduleKey, next, t('vault.fallbackDeleted'));
                            }}
                            className="shrink-0 flex items-center gap-1 px-2 py-1 rounded border border-line text-xs text-ink-2 hover:text-danger hover:border-danger transition-colors"
                            title={t('vault.fallbackRemove')}
                          >
                            <X className="h-3 w-3" />
                            {t('vault.fallbackRemove')}
                          </button>
                        </div>
                        {fbTesting === moduleKey + '#' + i && (
                          <div className="mt-1 text-xs text-ink-3">
                            {t('vault.fallbackTesting')}
                          </div>
                        )}
                        {fbResults[moduleKey + '#' + i] && (() => {
                          const r = fbResults[moduleKey + '#' + i]!;
                          const partial = r.success && r.verdict !== 'ok';
                          return (
                            <div className={'mt-1 text-xs ' + (r.success ? (partial ? 'text-warning-strong' : 'text-success-strong') : 'text-danger-strong')}>
                              {r.success
                                ? (partial ? t('vault.fallbackTestPartial') : t('vault.fallbackTestOk', { label: fb.label || t('vault.fallbackN', { n: i + 1 }) }))
                                : t('vault.fallbackTestFail') + '：' + sanitizeErrorMessage(String(r.error || '')).slice(0, 180)}
                            </div>
                          );
                        })()}
                      </li>
                    ))}
                  </ul>
                )}
                {fbDirty[moduleKey] && (
                  <div className="mt-2 flex items-start gap-1.5 text-xs text-warning-strong">
                    <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-[1px]" />
                    <span>{t('vault.fallbackSaveFailed')}</span>
                  </div>
                )}
                {showAddFb === moduleKey && (
                  <div className="mt-2 p-3 rounded-lg border border-line space-y-2">
                    {editingFb && editingFb.module === moduleKey && (
                      <div className="text-xs text-ink-2">{t('vault.fallbackEditTitle')}</div>
                    )}
                    <Input label="Label" placeholder={t('vault.fallbackLabelPh')} value={fbDraft.label || ''}
                      onChange={v => setFbDraft(d => ({ ...d, label: v }))} className="text-sm" />
                    <Input label="Base URL" placeholder={t('vault.fallbackUrlPh')} value={fbDraft.base_url || ''}
                      onChange={v => setFbDraft(d => ({ ...d, base_url: v }))} className="font-mono text-sm" />
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <ModelPicker
                          label="Model"
                          placeholder={t('vault.fallbackModelPh')}
                          value={fbDraft.model || ''}
                          onChange={v => setFbDraft(d => ({ ...d, model: v }))}
                          models={fbModelsState.models}
                        />
                      </div>
                      <Input label="API Key" type="password" placeholder={t('vault.fallbackKeyPh')}
                        value={fbDraft.api_key || ''}
                        onChange={v => setFbDraft(d => ({ ...d, api_key: v }))} className="font-mono text-sm" />
                    </div>
                    <div className="flex items-center gap-2">
                      <Button variant="secondary" size="sm" onClick={handleDetectFbModels} loading={fbModelsState.loading}>
                        {t('vault.detectModels')}
                      </Button>
                    </div>
                    {renderModelsStatus(fbModelsState)}
                    <div className="flex justify-end gap-2">
                      <Button variant="ghost" size="sm" onClick={closeFbForm}>
                        {t('vault.cancel')}
                      </Button>
                      <Button variant="brand" size="sm"
                        disabled={!fbDraft.base_url?.trim() || !fbDraft.model?.trim()}
                        onClick={() => {
                          const isEdit = Boolean(editingFb && editingFb.module === moduleKey);
                          const list = [...(fallbacks[moduleKey] || [])];
                          if (isEdit && editingFb) list[editingFb.index] = { ...fbDraft };
                          else list.push({ ...fbDraft });
                          setFallbacks(prev => ({ ...prev, [moduleKey]: list }));
                          setFbResults(prev => {
                            const next = { ...prev };
                            Object.keys(next).forEach(k => { if (k.startsWith(moduleKey + '#')) delete next[k]; });
                            return next;
                          });
                          closeFbForm();
                          persistFallbacks(moduleKey, list,
                            isEdit ? t('vault.fallbackUpdated') : t('vault.fallbackAdded'));
                        }}
                      >
                        {editingFb && editingFb.module === moduleKey ? t('vault.fallbackUpdate') : t('vault.fallbackSave')}
                      </Button>
                    </div>
                  </div>
                )}
              </div>

              {/* Action Buttons */}
              <div className="flex items-center gap-3 pt-2">
                <Button
                  variant="secondary"
                  onClick={() => handleTest(moduleKey)}
                  loading={testing === moduleKey}
                  disabled={!state.base_url || !state.model}
                >
                  {t('vault.testConnection')}
                </Button>
                <Button
                  variant="brand"
                  onClick={() => handleSave(moduleKey)}
                  loading={saving === moduleKey}
                  disabled={!state.base_url || !state.model}
                >
                  {t('vault.saveConfig')}
                </Button>
              </div>

              {/* Test Result */}
              {result && (() => {
                const partial = result.success && result.verdict !== 'ok';
                const ms = result.latency_ms ?? result.response_time_ms;
                const box = partial
                  ? 'bg-warning-subtle text-warning-strong'
                  : result.success
                    ? 'bg-success-subtle text-success-strong'
                    : 'bg-danger-subtle text-danger-strong';
                const ResultIcon = partial ? AlertTriangle : result.success ? CheckCircle2 : X;
                return (
                  <div className={'p-3 rounded-lg text-sm ' + box}>
                    <div className="flex items-center gap-2 flex-wrap">
                      <ResultIcon className="h-4 w-4 shrink-0" />
                      <span className="font-medium">
                        {partial ? t('vault.verdictReachableNoContent') : result.success ? t('vault.testSuccess') : t('vault.testFailed')}
                      </span>
                      {typeof ms === 'number' && (
                        <span className="text-xs opacity-75">({ms}ms)</span>
                      )}
                      {result.max_tokens != null && (
                        <span className="text-xs opacity-75">max_tokens={result.max_tokens}</span>
                      )}
                      {result.disable_thinking === false && (
                        <span className="text-xs opacity-75">{t('vault.thinkingOn')}</span>
                      )}
                      {result.vision === false && <span className="text-xs opacity-75">{t('vault.noVision')}</span>}
                      {result.vision === null && result.uncertain && (
                        <span className="text-xs opacity-75">{t('vault.visionUnconfirmed')}</span>
                      )}
                    </div>
                    {result.reply ? (
                      <p className="mt-1 text-xs opacity-75 break-all">{t('vault.modelReply', { reply: result.reply })}</p>
                    ) : null}
                    {result.hint && <p className="mt-1 text-xs opacity-75">{result.hint}</p>}
                    {!result.success && result.error && (
                      <p className="mt-1 text-xs opacity-75">{sanitizeErrorMessage(result.error)}</p>
                    )}
                    {!result.success && result.guide && (
                      <p className="mt-1 text-xs opacity-75">{result.guide}</p>
                    )}
                  </div>
                );
              })()}
            </Card>
          );
        })}
      </div>

      {/* ===== 提示词模板管理 ===== */}
      <PromptManagerTab />

      {/* Info Card */}
      <Card title={t('vault.infoTitle')} bodyClassName="p-4">
        <ul className="text-sm text-ink-2 space-y-1 list-disc list-inside">
          <li>{t('vault.infoIndependent')}</li>
          <li>{t('vault.infoTextModel')}</li>
          <li>{t('vault.infoQcModel')}</li>
          <li>{t('vault.infoComfyui')}</li>
          <li>{t('vault.infoApiKey')}</li>
          <li>{t('vault.infoTestFirst')}</li>
        </ul>
      </Card>

      <ConfirmDialog
        isOpen={clearTarget !== null}
        onClose={() => (clearing ? undefined : setClearTarget(null))}
        onConfirm={doClear}
        title={t('vault.clearConfig')}
        danger
        loading={clearing}
        confirmText={t('vault.clear')}
        message={
          clearTarget
            ? t('vault.clearMessage', { module: t(MODULE_CONFIG[clearTarget].titleKey) })
            : null
        }
      />
    </div>
  );
}
