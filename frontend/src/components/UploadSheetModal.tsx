/** UploadSheetModal —— E-5（2026-10-16）从 ProjectWorkbenchPage.tsx 抽出，逐字搬运。 */
import { useState, useEffect, useRef } from 'react';
import { useApp } from '@/context/AppContext';
import { characterSheetUpload, asOverwriteConfirm, overwriteScopeSummary, type OverwriteConfirmPayload } from '@/api/client';
import { Button, ConfirmDialog, Input, Modal, Select } from '@/components/ui';
import { useToast } from '@/components/ui/toast';


export function UploadSheetModal({
  isOpen,
  onClose,
  projectKey,
  characters,
  onUploaded,
}: {
  isOpen: boolean;
  onClose: () => void;
  projectKey: string;
  characters: string[];
  onUploaded?: () => void;
}) {
  const { t } = useApp();
  const toast = useToast();
  const [character, setCharacter] = useState('');
  const [customName, setCustomName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState('');
  const [busy, setBusy] = useState(false);
  const [overwrite, setOverwrite] = useState(false);
  /**
   * 后端 409「跨集覆盖未确认」带上来的影响范围（非空 = 确认框打开）。
   * 角色设定图是全项目共用基准：勾了「覆盖」上传 = 改写从第 1 集起的形象，
   * 必须先让用户看清影响范围再点头（t16 后端已拦，这里是配套的前端防线）。
   */
  const [overwriteConfirm, setOverwriteConfirm] = useState<OverwriteConfirmPayload | null>(null);
  const [confirming, setConfirming] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // 每次打开重置：上一次的文件/错误绝不能带进下一次（会误传给另一个角色）。
  // ⚠️ t17 实测修复：依赖里**不能再放 characters** —— 工作台在后台轮询刷新时 characters
  //    每次都是新数组引用，会让这条 effect 在弹窗打开期间反复执行，把用户已选的文件、
  //    已勾的「覆盖」静默清空（提交按钮随之变灰 → 用户以为「点了没反应」）。
  //    改用 ref 读最新角色列表，只在「打开/关闭」时才重置。
  const charsRef = useRef(characters);
  charsRef.current = characters;
  useEffect(() => {
    if (!isOpen) return;
    setFile(null);
    setPreview('');
    setBusy(false);
    setOverwrite(false);
    setOverwriteConfirm(null);
    setConfirming(false);
    setCustomName('');
    setCharacter((prev) => (prev && charsRef.current.includes(prev) ? prev : charsRef.current[0] || ''));
  }, [isOpen]);

  useEffect(() => {
    if (!file) {
      setPreview('');
      return;
    }
    const url = URL.createObjectURL(file);
    setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const finalName = (character === '__custom__' ? customName : character).trim();

  /** 本轮新增文案：locales 不在本任务范围（captain 统一补），缺 key 时回落中文，绝不显示裸 key */
  const tf = (key: string, fallback: string, params?: Record<string, string | number>) => {
    const v = t(key, params);
    if (v !== key) return v;
    let out = fallback;
    if (params) {
      Object.entries(params).forEach(([k, val]) => {
        out = out.replace(new RegExp(`\\{${k}\\}`, 'g'), String(val));
      });
    }
    return out;
  };
  const confirmSummary = overwriteConfirm ? overwriteScopeSummary(overwriteConfirm) : null;

  /**
   * 上传形象图。confirmOverwrite=true 只在**用户在确认框上点头之后**才用 ——
   * 这是后端的 confirm_field 语义，不是「重试」。
   */
  const submit = async (confirmOverwrite = false) => {
    if (!finalName) {
      toast.error(t('uploadSheet.needCharacter'));
      return;
    }
    if (!file) {
      toast.error(t('uploadSheet.needFile'));
      return;
    }
    setBusy(true);
    try {
      const r = await characterSheetUpload.upload({
        project_name: projectKey,
        character: finalName,
        file,
        overwrite,
        confirmOverwrite,
      });
      if (r.skipped) {
        toast.info(r.message || t('uploadSheet.skipped'));
      } else {
        // 2026-10-02 不裁剪：views 恒为空，改用后端返回的准确文案（否则会显示「0 张视角图」）
        toast.success(r.message || t('uploadSheet.ok', { n: Object.keys(r.views || {}).length }));
      }
      setOverwriteConfirm(null);
      onUploaded?.();
      onClose();
    } catch (e) {
      // ⭐ 跨集覆盖：后端 409 + 影响清单（code/needs_confirm 判定，不按状态码）→
      //    弹确认框把后果摆给用户，而不是把后端的长错误当成「失败」直接弹走。
      const info = asOverwriteConfirm(e);
      if (info) {
        setOverwriteConfirm(info);
        return;
      }
      // 后端把「版式不符」的可读原因 + layout_hint 都放在 error 里，直接展示
      toast.error(e instanceof Error ? e.message : t('uploadSheet.failed'));
    } finally {
      setBusy(false);
    }
  };

  /** 确认框点「确认覆盖」→ 带 confirm_overwrite=1 重发同一请求 */
  const submitConfirmed = async () => {
    setConfirming(true);
    try {
      await submit(true);
    } finally {
      setConfirming(false);
      setOverwriteConfirm(null);
    }
  };

  return (
    <>
    <Modal isOpen={isOpen} onClose={onClose} title={t('uploadSheet.title')}>
      <div className="space-y-4">
        {/* 版式示意：这是本功能唯一的使用门槛，必须一眼看懂 */}
        <div className="rounded-lg border border-line bg-surface-2 p-3">
          <p className="text-xs font-medium text-ink-1 mb-2">{t('uploadSheet.layoutTitle')}</p>
          <div className="flex items-center gap-2">
            <div className="flex flex-col gap-1">
              <div className="flex gap-1">
                {['front', 'left', 'back'].map((k) => (
                  <div
                    key={k}
                    className="w-12 h-16 rounded border border-dashed border-ink-3 bg-surface flex items-center justify-center text-xs text-ink-3"
                  >
                    {t(`uploadSheet.view.${k}`)}
                  </div>
                ))}
              </div>
              <div className="w-12 h-16 rounded border border-dashed border-ink-3 bg-surface flex items-center justify-center text-xs text-ink-3">
                {t('uploadSheet.view.half')}
              </div>
            </div>
            <p className="text-xs text-ink-2 flex-1">{t('uploadSheet.layoutHint')}</p>
          </div>
        </div>

        {/* 角色选择 */}
        <div>
          <label className="block text-sm font-medium text-ink-1 mb-1">
            {t('uploadSheet.character')}
          </label>
          <Select
            value={character === '__custom__' || !characters.includes(character) ? '__custom__' : character}
            onChange={(v) => setCharacter(v)}
            options={[
              ...characters.map((c) => ({ value: c, label: c })),
              { value: '__custom__', label: t('uploadSheet.customName') },
            ]}
            className="w-full"
          />
          {(character === '__custom__' || characters.length === 0) && (
            <Input
              className="mt-2 w-full"
              value={customName}
              placeholder={t('uploadSheet.characterPlaceholder')}
              onChange={(v) => setCustomName(v)}
            />
          )}
        </div>

        {/* 文件选择 */}
        <div>
          <label className="block text-sm font-medium text-ink-1 mb-1">
            {t('uploadSheet.file')}
          </label>
          <input
            ref={fileRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] || null)}
          />
          <div className="flex items-center gap-3">
            <Button variant="secondary" onClick={() => fileRef.current?.click()} disabled={busy}>
              {file ? t('uploadSheet.reselect') : t('uploadSheet.choose')}
            </Button>
            <span className="text-xs text-ink-2 truncate">
              {file ? `${file.name}（${(file.size / 1024).toFixed(0)} KB）` : t('uploadSheet.noFile')}
            </span>
          </div>
          {preview && (
            <div className="mt-2 rounded-lg border border-line overflow-hidden bg-surface-2">
              <img src={preview} alt="preview" className="w-full max-h-56 object-contain" />
            </div>
          )}
        </div>

        <label className="flex items-center gap-2 text-sm text-ink-2">
          <input
            type="checkbox"
            checked={overwrite}
            onChange={(e) => setOverwrite(e.target.checked)}
            className="rounded border-line"
          />
          {t('uploadSheet.overwrite')}
        </label>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => submit(false)} disabled={busy || !file || !finalName}>
            {busy ? t('uploadSheet.uploading') : t('uploadSheet.submit')}
          </Button>
        </div>
      </div>
    </Modal>

    {/* ⭐ 跨集覆盖确认框：把后端 409 的影响清单**如实**摆给用户，点头后才带 confirm_overwrite 重发。
        没有这个弹框时，用户看到的是后端那句长错误 —— 以为是失败，其实是保护。 */}
      <ConfirmDialog
        isOpen={!!overwriteConfirm}
        onClose={() => setOverwriteConfirm(null)}
        onConfirm={submitConfirmed}
        loading={confirming}
        danger
        title={tf('uploadSheet.overwriteConfirmTitle', '这张图会改写多集的人物形象，确认？')}
        confirmText={tf('uploadSheet.overwriteConfirmOk', '确认覆盖')}
        cancelText={t('common.cancel')}
        message={
          confirmSummary ? (
            <div className="space-y-2">
              <p>
                {tf(
                  'uploadSheet.overwriteConfirmBody',
                  '角色「{name}」的设定图是全项目共用基准：这次覆盖会同时改写第 {eps} 集的人物形象（共 {n} 个资产）。',
                  {
                    name: confirmSummary.assetsAffected[0] || confirmSummary.assets[0]?.name || finalName,
                    eps: confirmSummary.episodeText,
                    n: confirmSummary.assetCount,
                  }
                )}
              </p>
              {confirmSummary.assets.length > 0 && (
                <ul className="list-inside list-disc space-y-0.5 text-xs text-ink-3">
                  {confirmSummary.assets.map((a) => (
                    <li key={a.name}>
                      {tf('uploadSheet.overwriteConfirmRow', '{name}：第 {eps} 集', {
                        name: a.name,
                        eps: a.episodeText,
                      })}
                    </li>
                  ))}
                </ul>
              )}
              <p className="text-xs text-danger-strong">
                {tf(
                  'uploadSheet.overwriteConfirmWarn',
                  '确认后会立即用新图覆盖该角色在所有已出现集里的形象基准，且不可撤销。'
                )}
              </p>
            </div>
          ) : null
        }
      />
    </>
  );
}