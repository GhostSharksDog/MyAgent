import { useEffect, useRef, useState } from 'react'
import { mcpRequest, newMCPConfig, stringMap } from '../../lib/mcp'
import type { MCPConfig, MCPServerView, MCPView } from '../../lib/mcp'

export function MCPSection({ onChanged }: { onChanged?: () => void }) {
  const [view, setView] = useState<MCPView | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [draft, setDraft] = useState<MCPConfig | null>(null)
  const [args, setArgs] = useState('[]')
  const [headers, setHeaders] = useState('{}')
  const [env, setEnv] = useState('{}')
  const generation = useRef(0)
  const mounted = useRef(true)
  const firstInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    const version = ++generation.current
    setBusy(true)
    mcpRequest('', 'GET', undefined, controller.signal).then(value => {
      if (version === generation.current) setView(value)
    }).catch(reason => {
      if (!controller.signal.aborted && version === generation.current) setError(String(reason.message ?? reason))
    }).finally(() => { if (version === generation.current) setBusy(false) })
    return () => { mounted.current = false; ++generation.current; controller.abort() }
  }, [])
  useEffect(() => { if (draft) firstInput.current?.focus() }, [draft?.id, draft === null])

  async function perform(path = '', method = 'GET', payload?: unknown, closeDraft = false) {
    if (busy) return
    const version = ++generation.current
    setBusy(true); setError(''); setNotice('')
    try {
      const value = await mcpRequest(path, method, payload)
      if (!mounted.current || version !== generation.current) return
      setView(value)
      if (closeDraft) setDraft(null)
      setNotice(method === 'GET' ? '已刷新服务状态' : '操作已完成，状态已刷新')
      onChanged?.()
    } catch (reason) {
      if (mounted.current && version === generation.current) setError(reason instanceof Error ? reason.message : 'MCP 请求失败')
    } finally { if (mounted.current && version === generation.current) setBusy(false) }
  }
  function edit(value: MCPConfig) {
    setError(''); setNotice('')
    const { id, name, transport, enabled, url, command, args, cwd, env, headers, proxy, selected_tools } = value
    setDraft({ id, name, transport, enabled, url, command, args, cwd, env, headers, proxy, selected_tools })
    setArgs(JSON.stringify(args, null, 2)); setHeaders(JSON.stringify(headers, null, 2)); setEnv(JSON.stringify(env, null, 2))
  }
  function save() {
    if (!draft) return
    try {
      const parsed: unknown = JSON.parse(args)
      if (!Array.isArray(parsed) || parsed.some(v => typeof v !== 'string')) throw new Error('启动参数须为字符串数组，例如 ["server.py"]')
      void perform('/servers', 'PUT', { ...draft, args: parsed, headers: stringMap(headers), env: stringMap(env) }, true)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'JSON 格式无效') }
  }
  function selection(server: MCPServerView, name: string, selected: boolean, trusted: boolean) {
    const names = new Set(server.selected_tools)
    if (selected) names.add(name); else names.delete(name)
    const trusts = new Set(server.tools.filter(t => t.trusted && names.has(t.name)).map(t => t.name))
    if (trusted && selected) trusts.add(name); else trusts.delete(name)
    void perform(`/servers/${encodeURIComponent(server.id)}/tools`, 'PUT', { selected: [...names], trusted: [...trusts] })
  }

  return <section className="settings__group" aria-label="MCP 服务设置">
    <h3 className="settings__legend">连接外部工具</h3>
    <p className="settings__hint settings__hint--block">为三种 Agent 模式接入搜索、网页读取和其它工具。默认关闭；先保存服务，再测试连接并选择工具。</p>
    {error && <p className="settings__alert settings__alert--bad" role="alert">{error}</p>}
    {view?.error && <p className="settings__alert settings__alert--bad" role="alert">{view.error}</p>}
    {notice && <p className="settings__result" role="status">{notice}</p>}
    {busy && <p role="status">正在处理 MCP 配置…</p>}
    <fieldset disabled={busy} className="mcp-fields">
      {view && <label className="settings__field settings__field--check"><input type="checkbox" checked={view.enabled}
        onChange={e => void perform('/config', 'PATCH', { enabled: e.target.checked })} /><span>启用 MCP（保存并立即生效）</span></label>}
      <div className="settings__actions">
        <button type="button" className="btn" onClick={() => edit(newMCPConfig())}>添加 MCP 服务</button>
        <button type="button" className="btn" onClick={() => edit(newMCPConfig(true))}>添加 Exa 搜索示例</button>
        <button type="button" className="btn btn--ghost" onClick={() => void perform()}>刷新 MCP 状态</button>
      </div>
      {draft && <section className="mcp-service" aria-label="编辑 MCP 服务">
        <h4>{draft.id ? '编辑服务' : '新服务'}</h4>
        <label className="settings__field"><span>服务名称</span><input ref={firstInput} className="settings__input" value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
        <label className="settings__field"><span>连接方式</span><select className="settings__input" value={draft.transport} onChange={e => setDraft({ ...draft, transport: e.target.value as 'http' | 'stdio' })}><option value="http">远程服务（Streamable HTTP）</option><option value="stdio">本地程序（stdio）</option></select></label>
        {draft.transport === 'http' ? <>
          <label className="settings__field"><span>MCP 地址</span><input className="settings__input" value={draft.url} placeholder="https://example.com/mcp" onChange={e => setDraft({ ...draft, url: e.target.value })} /></label>
          <label className="settings__field"><span>鉴权请求头（JSON）</span><textarea className="settings__input" rows={3} spellCheck={false} autoComplete="off" value={headers} onChange={e => setHeaders(e.target.value)} /></label>
          <label className="settings__field"><span>代理地址（可选）</span><input className="settings__input" autoComplete="off" type="password" value={draft.proxy} onChange={e => setDraft({ ...draft, proxy: e.target.value })} /></label>
        </> : <>
          <label className="settings__field"><span>启动程序</span><input className="settings__input" value={draft.command} onChange={e => setDraft({ ...draft, command: e.target.value })} /></label>
          <label className="settings__field"><span>启动参数（JSON 数组）</span><textarea className="settings__input" rows={3} value={args} onChange={e => setArgs(e.target.value)} /></label>
          <label className="settings__field"><span>工作目录（绝对路径）</span><input className="settings__input" value={draft.cwd} onChange={e => setDraft({ ...draft, cwd: e.target.value })} /></label>
          <label className="settings__field"><span>环境变量（JSON）</span><textarea className="settings__input" rows={3} spellCheck={false} autoComplete="off" value={env} onChange={e => setEnv(e.target.value)} /></label>
          <p className="settings__hint">测试连接会启动此程序。程序拥有服务账户权限，工作目录不是沙箱；请只配置你信任的服务和允许访问的目录。</p>
        </>}
        <p className="settings__hint">已保存的密钥仅显示掩码；保留掩码表示不修改。服务清单只保存在后端本机，不上传配置。</p>
        <div className="settings__actions"><button type="button" className="btn btn--primary" onClick={save}>保存 MCP 服务</button><button type="button" className="btn" onClick={() => setDraft(null)}>取消编辑</button></div>
      </section>}
      {view?.servers.map(server => <section key={server.id} className="mcp-service" aria-label={`MCP 服务：${server.name}`}>
        <h4>{server.name} <small>{server.status === 'connected' ? '已连接' : server.status === 'error' ? '连接失败' : '未连接'}</small></h4>
        <p className="settings__hint">{server.transport === 'http' ? server.url : `${server.command} · ${server.cwd}`}{server.protocol && ` · 协议 ${server.protocol}`}</p>
        {server.error && <p role="alert" className="settings__alert">{server.error}</p>}
        <div className="settings__actions">
          <button type="button" className="btn" onClick={() => void perform(`/servers/${encodeURIComponent(server.id)}/test`, 'POST')}>测试连接／刷新工具</button>
          <button type="button" className="btn" onClick={() => edit(server)}>编辑服务</button>
          <button type="button" className="btn btn--ghost" onClick={() => void perform(`/servers/${encodeURIComponent(server.id)}`, 'DELETE')}>删除服务</button>
        </div>
        <label className="settings__field settings__field--check"><input type="checkbox" checked={server.enabled} onChange={e => {
          const { status: _status, error: _error, protocol: _protocol, tools: _tools, ...config } = server
          void perform('/servers', 'PUT', { ...config, enabled: e.target.checked })
        }} /><span>向 Agent 提供此服务的已选工具</span></label>
        {server.tools.map(tool => <div className="mcp-tool" key={tool.name}>
          <label className="settings__field settings__field--check"><input type="checkbox" checked={tool.selected} disabled={!!tool.error} onChange={e => selection(server, tool.name, e.target.checked, tool.trusted)} /><span>{tool.name}</span></label>
          <p className="settings__hint">{tool.description}</p>
          {tool.error ? <p role="alert">{tool.error}</p> : tool.trust_allowed ? <label className="settings__field settings__field--check"><input type="checkbox" checked={tool.trusted} disabled={!tool.selected} onChange={e => selection(server, tool.name, tool.selected, e.target.checked)} /><span>我信任此只读工具，允许免确认调用</span></label> : <p className="settings__hint">每次调用均需确认</p>}
        </div>)}
      </section>)}
    </fieldset>
    <p className="settings__hint settings__hint--block">外部工具的访问范围由服务配置决定；内置文件与终端权限不限制外部服务。授权绑定具体工具定义，定义改变后需重新确认。Exa 免密钥入口有频率限制。</p>
  </section>
}
