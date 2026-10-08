import { useEffect, useRef, useState } from 'react'
import { mcpRequest } from '../../lib/mcp'
import type { MCPConfig, MCPServerView, MCPView } from '../../lib/mcp'
import { applySecret, detectPreset, MCP_PRESETS, presetDraft } from '../../lib/mcp-draft'

function MapEditor({ label, value, onChange }: { label: string; value: Record<string, string>; onChange: (v: Record<string, string>) => void }) {
  const entries = Object.entries(value)
  return <div className="mcp-map"><span>{label}</span>{entries.map(([key, text], index) => <div className="mcp-map__row" key={index}>
    <input aria-label={`${label}名称 ${index + 1}`} className="settings__input" value={key} onChange={e => { const next = [...entries]; next[index] = [e.target.value, text]; onChange(Object.fromEntries(next)) }} />
    <input aria-label={`${label}值 ${index + 1}`} className="settings__input" type="password" autoComplete="off" value={text === '********' ? '' : text} placeholder={text === '********' ? '已保存 · 留空保留' : '值'} onChange={e => onChange({ ...value, [key]: e.target.value || (text === '********' ? text : '') })} />
    <button className="btn btn--ghost" type="button" aria-label={`删除${label} ${index + 1}`} onClick={() => onChange(Object.fromEntries(entries.filter((_, i) => i !== index)))}>移除</button>
  </div>)}<button type="button" className="btn" onClick={() => onChange({ ...value, [`NEW_${entries.length + 1}`]: '' })}>添加{label}</button></div>
}

export function MCPSection({ onChanged }: { onChanged?: () => void }) {
  const [view, setView] = useState<MCPView | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<MCPConfig | null>(null)
  const [key, setKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [auth, setAuth] = useState<'none' | 'bearer' | 'header'>('none')
  const [headerName, setHeaderName] = useState('X-API-Key')
  const [advanced, setAdvanced] = useState(false)
  const generation = useRef(0)
  const mounted = useRef(true)
  const firstInput = useRef<HTMLInputElement>(null)
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    const version = ++generation.current
    setBusy(true)
    mcpRequest('', 'GET', undefined, controller.signal).then(v => { if (version === generation.current) setView(v) })
      .catch(e => { if (!controller.signal.aborted && version === generation.current) setError(e.message) })
      .finally(() => { if (version === generation.current) setBusy(false) })
    return () => { mounted.current = false; ++generation.current; controller.abort() }
  }, [])
  useEffect(() => { if (draft) firstInput.current?.focus() }, [draft?.id, draft === null])
  async function perform(path = '', method = 'GET', payload?: unknown, finish = false) {
    if (busy) return
    const version = ++generation.current
    setBusy(true); setError(''); setNotice('')
    try {
      const next = await mcpRequest(path, method, payload)
      if (!mounted.current || version !== generation.current) return
      setView(next); onChanged?.()
      if (path.endsWith('/tools')) {
        setDraft(previous => {
          const server = next.servers.find(s => s.id === previous?.id)
          return previous && server ? { ...previous, selected_tools: [...server.selected_tools] } : previous
        })
      }
      if (finish) {
        const server = next.servers.find(s => s.id === draft?.id) ?? next.servers.at(-1)
        if (server?.status === 'connected') { setDraft(null); setNotice('已保存并连接') }
        else {
          if (server) setDraft(previous => previous ? { ...previous, id: server.id } : previous)
          setError(server?.error || '配置已保存，连接尚未成功。请检查地址或密钥，然后重试。')
        }
      } else setNotice('状态已更新')
    } catch (e) { if (mounted.current && version === generation.current) setError(e instanceof Error ? e.message : '操作失败，请重试') }
    finally { if (mounted.current && version === generation.current) setBusy(false) }
  }
  function edit(config: MCPConfig) {
    const { id, name, transport, enabled, url, command, args, cwd, env, headers, proxy, selected_tools } = config
    const preset = detectPreset(config)
    setDraft({ id, name, transport, enabled, url, command, args: [...args], cwd, env: { ...env }, headers: { ...headers }, proxy, selected_tools: [...selected_tools], preset })
    setKey(''); setClearKey(false); setAdvanced(false); setError(''); setNotice('')
    const customHeader = Object.keys(headers).find(v => v.toLowerCase() !== 'authorization')
    setAuth(preset === 'tavily' || headers.Authorization ? 'bearer' : customHeader ? 'header' : 'none')
    setHeaderName(customHeader || 'X-API-Key')
  }
  function save() {
    if (!draft) return
    try {
      const local = draft.preset === 'filesystem' || draft.preset === 'desktop_commander'
      const headers = applySecret(draft.headers, key, auth, headerName, clearKey)
      if (draft.preset === 'tavily' && !headers.Authorization && !clearKey) throw new Error('API Key：请填写 Tavily 密钥')
      void perform('/servers/connect', 'PUT', { ...draft, headers, ...(local ? { workspace: draft.cwd } : {}) }, true)
    } catch (e) { setError(e instanceof Error ? e.message : '请检查配置项') }
  }
  function selection(server: MCPServerView, name: string, selected: boolean, trusted: boolean) {
    const names = new Set(server.selected_tools)
    if (selected) names.add(name); else names.delete(name)
    const trusts = new Set(server.tools.filter(t => t.trusted && names.has(t.name)).map(t => t.name))
    if (trusted && selected) trusts.add(name); else trusts.delete(name)
    void perform(`/servers/${encodeURIComponent(server.id)}/tools`, 'PUT', { selected: [...names], trusted: [...trusts] })
  }
  const local = draft?.preset === 'filesystem' || draft?.preset === 'desktop_commander'
  const current = view?.servers.find(s => s.id === draft?.id)
  return <section className="settings__group" aria-label="MCP 服务设置">
    <h3 className="settings__legend">外部工具</h3>
    <p className="settings__hint settings__hint--block">按需添加搜索、文件或终端服务。每次调用默认需要你确认。</p>
    {(error || view?.error) && <p className="settings__alert settings__alert--bad" role="alert">{error || view?.error}</p>}
    {notice && <p className="settings__result" role="status">{notice}</p>}
    {busy && <p role="status">正在连接或保存…</p>}
    <fieldset disabled={busy} className="mcp-fields">
      {!draft ? <>
        <div className="mcp-presets">{MCP_PRESETS.map(p => <button type="button" key={p.id} className="btn" onClick={() => edit(presetDraft(p.id))}>添加 {p.label}</button>)}</div>
        {view?.servers.map(s => <div className="mcp-service mcp-service--compact" key={s.id} aria-label={`MCP 服务：${s.name}`}>
          <div><strong>{s.name}</strong><p className="settings__hint">{s.status === 'connected' && s.enabled && view.enabled ? `已连接 · ${s.available_tool_count ?? s.tools.filter(t => t.selected && !t.error).length} 项工具可用` : s.status === 'error' ? '连接失败' : '未连接'}</p>
            {!!s.missing_tools?.length && <p className="settings__alert settings__alert--bad" role="alert">所选工具未找到：{s.missing_tools.join('、')}。请进入配置 → 高级设置，刷新并选择实际工具。</p>}
            {s.status === 'connected' && s.enabled && view.enabled && (s.available_tool_count ?? s.tools.filter(t => t.selected && !t.error).length) === 0 && <p className="settings__hint">当前服务尚未向 Agent 提供工具，请在高级设置选择工具。</p>}
            {s.error && <p className="settings__alert settings__alert--bad" role="alert">{s.error}</p>}
          </div>
          <div className="settings__actions"><button type="button" className="btn" onClick={() => edit(s)}>配置</button>
            <button type="button" className="btn btn--ghost" onClick={() => void perform(`/servers/${encodeURIComponent(s.id)}/enabled`, 'PATCH', { enabled: !s.enabled })}>{s.enabled ? '停用' : '连接'}</button></div>
        </div>)}
        {view?.servers.length === 0 && <p className="settings__hint">尚未添加服务。基础聊天不需要配置 MCP。</p>}
        <button type="button" className="btn btn--ghost" onClick={() => void perform()}>刷新状态</button>
      </> : <div className="mcp-service" aria-label="编辑 MCP 服务">
        <h4>{MCP_PRESETS.find(p => p.id === draft.preset)?.label ?? '服务配置'}</h4>
        <label className="settings__field"><span>名称</span><input ref={firstInput} className="settings__input" value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
        {local ? <><label className="settings__field"><span>允许访问的目录</span><input className="settings__input" placeholder="例如 D:\我的文件" value={draft.cwd} onChange={e => setDraft({ ...draft, cwd: e.target.value })} /></label>
          <p className="settings__hint">程序随 Windows 发行包提供。外部服务的范围由此配置决定；终端命令拥有当前账户权限，目录不是终端沙箱。</p></> : <>
          {draft.preset !== 'tavily' && draft.preset !== 'exa' && <label className="settings__field"><span>连接方式</span><select className="settings__input" value={draft.transport} onChange={e => setDraft({ ...draft, transport: e.target.value as 'http' | 'stdio' })}><option value="http">远程 HTTP</option><option value="stdio">本地程序</option></select></label>}
          {draft.transport === 'http' ? <><label className="settings__field"><span>服务地址</span><input className="settings__input" value={draft.url} onChange={e => setDraft({ ...draft, url: e.target.value })} /></label>
            {draft.preset === 'custom' && <label className="settings__field"><span>鉴权方式</span><select className="settings__input" value={auth} onChange={e => { setAuth(e.target.value as typeof auth); setClearKey(e.target.value === 'none') }}><option value="none">无需密钥</option><option value="bearer">Bearer API Key</option><option value="header">自定义鉴权头</option></select></label>}
            {auth === 'header' && <label className="settings__field"><span>鉴权头名称</span><input className="settings__input" value={headerName} onChange={e => setHeaderName(e.target.value)} /></label>}
            {auth !== 'none' && <label className="settings__field"><span>API Key</span><input className="settings__input" type="password" autoComplete="off" value={key} placeholder={Object.keys(draft.headers).length ? '已保存 · 留空保留' : '填写 API Key'} onChange={e => { setKey(e.target.value); if (e.target.value.trim()) setClearKey(false) }} /></label>}
            {Object.keys(draft.headers).length > 0 && <label className="settings__field settings__field--check"><input type="checkbox" checked={clearKey} onChange={e => setClearKey(e.target.checked)} /><span>明确清除已保存的鉴权密钥</span></label>}
            {draft.preset === 'exa' && <p className="settings__hint">免密钥入口有频率限制；可在高级设置中添加鉴权。</p>}
          </> : <><label className="settings__field"><span>启动程序</span><input className="settings__input" value={draft.command} onChange={e => setDraft({ ...draft, command: e.target.value })} /></label><label className="settings__field"><span>工作目录</span><input className="settings__input" value={draft.cwd} onChange={e => setDraft({ ...draft, cwd: e.target.value })} /></label></>}
        </>}
        <details open={advanced} onToggle={e => setAdvanced(e.currentTarget.open)} className="mcp-advanced"><summary>高级设置</summary>
          {draft.transport === 'stdio' && !local && <><span>启动参数</span>{draft.args.map((arg, i) => <div className="mcp-map__row" key={i}><input aria-label={`启动参数 ${i + 1}`} className="settings__input" value={arg} onChange={e => setDraft({ ...draft, args: draft.args.map((v, n) => n === i ? e.target.value : v) })} /><button className="btn" type="button" onClick={() => setDraft({ ...draft, args: draft.args.filter((_, n) => n !== i) })}>移除</button></div>)}<button type="button" className="btn" onClick={() => setDraft({ ...draft, args: [...draft.args, ''] })}>添加参数</button><MapEditor label="环境变量" value={draft.env} onChange={env => setDraft({ ...draft, env })} /></>}
          {draft.transport === 'http' && <><MapEditor label="请求头" value={draft.headers} onChange={headers => setDraft({ ...draft, headers })} /><label className="settings__field"><span>代理地址</span><input className="settings__input" type="password" autoComplete="off" value={draft.proxy === '********' ? '' : draft.proxy} placeholder={draft.proxy ? '已保存 · 留空保留' : '可选'} onChange={e => setDraft({ ...draft, proxy: e.target.value || (draft.proxy === '********' ? draft.proxy : '') })} /></label></>}
          {current && <><button type="button" className="btn" onClick={() => void perform(`/servers/${encodeURIComponent(current.id)}/test`, 'POST')}>测试连接／刷新工具</button>
            {current.tools.map(t => <div className="mcp-tool" key={t.name}><label className="settings__field settings__field--check"><input type="checkbox" checked={t.selected} disabled={!!t.error} onChange={e => selection(current, t.name, e.target.checked, t.trusted)} /><span>{t.name}</span></label><p className="settings__hint">{t.description}</p>
              {t.trust_allowed && <label className="settings__field settings__field--check"><input type="checkbox" checked={t.trusted} disabled={!t.selected} onChange={e => selection(current, t.name, t.selected, e.target.checked)} /><span>我明确授权此只读工具免确认，并允许并发</span></label>}{t.error && <p role="alert">{t.error}</p>}</div>)}
            <button type="button" className="btn btn--danger" onClick={() => { if (window.confirm(`删除 ${current.name} 的配置？`)) { setDraft(null); void perform(`/servers/${encodeURIComponent(current.id)}`, 'DELETE') } }}>删除服务</button></>}
          <p className="settings__hint">只读授权绑定工具定义，定义变化后需要重新确认。已知写入、终端及未知能力始终逐次确认。</p>
        </details>
        <div className="settings__actions"><button type="button" className="btn btn--primary" onClick={save}>保存并连接</button><button type="button" className="btn" onClick={() => setDraft(null)}>返回服务列表</button></div>
      </div>}
    </fieldset>
  </section>
}
