import { useEffect, useRef, useState } from 'react'
import { PROVIDERS, presetFor } from '../lib/models-view'
import { storageRequest } from '../lib/storage-api'
import { useDialogFocus } from '../hooks/useDialogFocus'

export function SetupDialog({ onSaved }: { onSaved: () => void }) {
  const [open, setOpen] = useState(false)
  const [provider, setProvider] = useState('deepseek')
  const [baseUrl, setBaseUrl] = useState(presetFor('deepseek').baseUrl)
  const [model, setModel] = useState(presetFor('deepseek').model)
  const [key, setKey] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [persistent, setPersistent] = useState<boolean | null>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  const firstRef = useRef<HTMLSelectElement>(null)
  const mounted = useRef(true)
  useDialogFocus({ open, containerRef: dialogRef, onClose: () => {}, initialFocusRef: firstRef })
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    storageRequest<{ required: boolean; session_persistent?: boolean }>('setup', 'GET', undefined, controller.signal)
      .then(value => { if (mounted.current) { setOpen(value.required); setPersistent(value.session_persistent ?? null) } })
      .catch(e => { if (!controller.signal.aborted) { setError(e.message); setOpen(true) } })
    return () => { mounted.current = false; controller.abort() }
  }, [])
  async function save() {
    setBusy(true); setError('')
    try {
      await storageRequest('setup', 'POST', { provider, base_url: baseUrl, model, api_key: key })
      if (mounted.current) { setKey(''); setOpen(false); onSaved() }
    } catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : '保存失败，请重试') }
    finally { if (mounted.current) setBusy(false) }
  }
  if (!open) return null
  return <div className="dialog-scrim"><div className="setup-dialog" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="setup-title">
    <span className="setup-dialog__brand">Legacy</span>
    <h2 id="setup-title">连接模型，开始使用</h2>
    <p className="settings__hint">{persistent === false ? '当前对话仅存在本机内存，重启后不会保留；可在记忆与存储中查看配置。' : persistent === true ? '对话和已确认的记忆保存在本机。' : '请在记忆与存储中查看实际保存状态。'}文件、终端和联网服务可以之后按需配置。</p>
    <form onSubmit={e => { e.preventDefault(); void save() }}>
      <fieldset disabled={busy} className="mcp-fields">
        <label className="settings__field"><span>供应商</span><select ref={firstRef} className="settings__input" value={provider} onChange={e => { const preset = presetFor(e.target.value); setProvider(preset.id); setBaseUrl(preset.baseUrl); setModel(preset.model) }}>{PROVIDERS.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}</select></label>
        <label className="settings__field"><span>API 地址</span><input required className="settings__input" type="url" value={baseUrl} onChange={e => setBaseUrl(e.target.value)} /></label>
        <label className="settings__field"><span>模型名</span><input required className="settings__input" value={model} onChange={e => setModel(e.target.value)} /></label>
        <label className="settings__field"><span>API Key</span><input required className="settings__input" type="password" autoComplete="off" placeholder={presetFor(provider).keyHint} value={key} onChange={e => setKey(e.target.value)} /></label>
        {error && <p className="settings__alert settings__alert--bad" role="alert">{error}</p>}
        <button className="btn btn--primary setup-dialog__submit" type="submit">{busy ? '正在保存…' : '保存并开始使用'}</button>
      </fieldset>
    </form>
    <p className="settings__hint">保存仅写入配置，不发送付费测试请求。</p>
  </div></div>
}
