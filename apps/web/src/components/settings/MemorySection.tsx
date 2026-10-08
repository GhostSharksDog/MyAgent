import { useEffect, useRef, useState } from 'react'
import { storageRequest } from '../../lib/storage-api'
import type { MemoryFact, MemoryList, StorageStatus } from '../../lib/storage-api'

export function MemorySection({ onChanged }: { onChanged?: () => void }) {
  const [view, setView] = useState<MemoryList | null>(null)
  const [status, setStatus] = useState<StorageStatus | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [text, setText] = useState('')
  const mounted = useRef(true)
  const generation = useRef(0)
  async function refresh(signal?: AbortSignal) {
    const version = ++generation.current
    try {
      const [memory, stores] = await Promise.all([storageRequest<MemoryList>('memory', 'GET', undefined, signal), storageRequest<StorageStatus>('storage', 'GET', undefined, signal)])
      if (mounted.current && version === generation.current) { setView(memory); setStatus(stores); setError('') }
    } catch (e) { if (!signal?.aborted && mounted.current && version === generation.current) setError(e instanceof Error ? e.message : '读取失败') }
  }
  useEffect(() => { mounted.current = true; const c = new AbortController(); void refresh(c.signal); return () => { mounted.current = false; ++generation.current; c.abort() } }, [])
  async function perform(path: string, method: string, payload?: unknown) {
    if (busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      await storageRequest(path, method, payload)
      if (!mounted.current) return
      await refresh()
      setNotice('已保存到本机'); onChanged?.()
      if (path.startsWith('memory')) { setText(''); setEditing(null) }
    } catch (e) { if (mounted.current) setError(e instanceof Error ? e.message : '保存失败，草稿已保留') }
    finally { if (mounted.current) setBusy(false) }
  }
  function edit(fact: MemoryFact) { setEditing(fact.id); setText(fact.text) }
  return <section className="settings__group" aria-label="记忆与存储">
    <h3 className="settings__legend">记忆与存储</h3>
    {error && <p className="settings__alert settings__alert--bad" role="alert">{error}<button className="btn" onClick={() => void refresh()}>重试读取</button></p>}
    {notice && <p className="settings__result" role="status">{notice}</p>}
    {status && <div className="storage-summary"><strong>{status.sessions.persistent ? '对话已持久保存' : '对话仅保存在内存'}</strong>
      <p className="settings__hint">{status.sessions.persistent ? (status.sessions.ttl_seconds === 0 ? '不过期，重启后仍保留。' : `最近活动后 ${Math.ceil(status.sessions.ttl_seconds / 86400)} 天过期。`) : '重启后对话历史不会保留。源码运行可配置 SESSION_BACKEND=sql。'}</p>
      <p className="settings__hint">长期记忆：{status.memory?.active_backend === 'sql' ? 'SQLite' : status.memory?.active_backend === 'json' ? 'JSON 文件' : '尚未启用'} · 运行摘要：{status.runs?.active_backend ?? '未知'}</p>
      <details><summary>查看保存位置</summary><code>{status.data_directory}</code></details>
    </div>}
    <fieldset disabled={busy || !view} className="mcp-fields">
      <label className="settings__field settings__field--check"><input type="checkbox" checked={view?.enabled ?? false} onChange={e => void perform('settings', 'PUT', { memory_enabled: e.target.checked })} /><span>使用长期记忆 · 立即生效</span></label>
      <p className="settings__hint">只保存你明确确认的内容。关闭后停止召回和新增，已有记忆保留。</p>
      <label className="settings__field settings__field--check"><input type="checkbox" checked={status?.memory?.enable_summary ?? true} onChange={e => void perform('settings', 'PUT', { memory_enable_summary: e.target.checked })} /><span>自动压缩较早的会话历史 · 下一轮生效</span></label>
      <p className="settings__hint">摘要会使用当前模型，消耗本轮时间与 token 预算；完整对话仍保留。Plan 和 Supervisor 不使用过去对话。</p>
      <form onSubmit={e => { e.preventDefault(); if (text.trim()) void perform(editing ? `memory/${editing}` : 'memory', editing ? 'PUT' : 'POST', { text, tags: view?.facts.find(f => f.id === editing)?.tags ?? [] }) }}>
        <label className="settings__field"><span>{editing ? '编辑记忆' : '添加一条希望记住的内容'}</span><textarea required maxLength={500} rows={3} className="settings__input" value={text} onChange={e => setText(e.target.value)} /></label>
        <div className="settings__actions"><button type="submit" className="btn btn--primary" disabled={!text.trim() || (!view?.enabled && !editing)}>{editing ? '保存修改' : '确认并保存记忆'}</button>{editing && <button className="btn" type="button" onClick={() => { setEditing(null); setText('') }}>取消编辑</button>}</div>
      </form>
      {view?.facts.map(f => <div className="memory-fact" key={f.id}><p>{f.text}</p><div className="settings__actions"><button className="btn" type="button" onClick={() => edit(f)}>编辑</button><button className="btn btn--ghost" type="button" onClick={() => { if (window.confirm('删除这条记忆？')) void perform(`memory/${f.id}`, 'DELETE') }}>删除</button></div></div>)}
      {view?.facts.length === 0 && <p className="settings__hint">还没有长期记忆。不会自动提取聊天内容。</p>}
      {!!view?.facts.length && <button className="btn btn--ghost" type="button" onClick={() => { if (window.confirm('清空所有长期记忆？此操作无法撤销。')) void perform('memory?confirm=true', 'DELETE') }}>清空全部记忆</button>}
    </fieldset>
  </section>
}
