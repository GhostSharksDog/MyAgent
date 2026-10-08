/* Browser-only public MCP fixtures. Never launches a program or calls a remote tool. */
(() => {
  const original = window.fetch.bind(window), f = window.__fixture;
  const json = body => Response.json(body);
  f.mcp = { enabled: false, error: '', max_tools: 32, servers: [] };
  f.mcpDecisions = [];
  f.restoreMCP = () => { window.fetch = original; };
  let sequence = 0;
  window.fetch = async (input, options = {}) => {
    const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
    const payload = JSON.parse(options.body || '{}');
    if (path.startsWith('/api/mcp')) {
      if (path.endsWith('/config')) f.mcp.enabled = payload.enabled;
      else if (path === '/api/mcp/servers' || path === '/api/mcp/servers/connect') {
        f.mcpSubmitted = payload;
        const names = payload.preset === 'tavily' ? ['tavily_search', 'tavily_extract'] : ['web_search_exa', 'web_fetch_exa'];
        const item = { ...payload, headers: Object.fromEntries(Object.keys(payload.headers || {}).map(k => [k, '********'])), id: payload.id || 'public-exa', enabled: true, status: f.mcp.failConnect ? 'error' : 'connected', error: f.mcp.failConnect ? 'API Key：密钥无效，请检查后重新连接。' : '', protocol: '', tools: names.map(name => ({ name, description: '公开合成工具', selected: payload.selected_tools.includes(name), trusted: false, trust_allowed: true, error: '' })) };
        f.mcp.enabled = true;
        f.mcp.servers = [...f.mcp.servers.filter(s => s.id !== item.id), item];
      } else if (path.endsWith('/enabled')) {
        f.mcp.servers[0].enabled = payload.enabled;
        f.mcp.enabled = payload.enabled;
        f.mcp.servers[0].status = payload.enabled ? 'connected' : 'disabled';
      } else if (path.endsWith('/test')) {
        const s = f.mcp.servers[0];
        s.status = 'connected'; s.protocol = '2026-07-28';
        s.tools = ['web_search_exa', 'web_fetch_exa'].map(name => ({ name, description: '公开合成工具', selected: true, trusted: false, trust_allowed: true, error: '' }));
      } else if (path.endsWith('/tools')) {
        const s = f.mcp.servers[0]; s.selected_tools = payload.selected;
        s.tools.forEach(t => { t.selected = payload.selected.includes(t.name); t.trusted = payload.trusted.includes(t.name); });
      } else if (options.method === 'DELETE') f.mcp.servers = [];
      f.mcp.servers.forEach(s => {
        s.available_tool_count = f.mcp.enabled && s.enabled && s.status === 'connected' ? s.tools.filter(t => t.selected && !t.error).length : 0;
        s.missing_tools = s.selected_tools.filter(n => !s.tools.some(t => t.name === n));
      });
      return json(f.mcp);
    }
    if (path === '/api/chat/stream' && f.case === 'mcp') {
      f.mcpCancelled = false;
      const id = 'mcp-' + (++sequence);
      const proposal = { id, kind: 'mcp', server_id: 'public-exa', server_name: 'Exa 联网', tool_name: 'web_search_exa',
        arguments: { query: '公开 MCP 文档', numResults: 2 }, fingerprint: 'a'.repeat(64), started: false, status: 'pending', message: '公开合成预览，尚未调用' };
      let controller, closed = false;
      const emit = event => { if (!closed) controller.enqueue(new TextEncoder().encode('event: ' + event.type + '\ndata: ' + JSON.stringify({ ...event, run_id: id }) + '\n\n')); };
      const body = new ReadableStream({ start(c) { controller = c; emit({ type: 'start' }); emit({ type: 'approval_request', approval: proposal }); }, cancel() { closed = true; f.mcpCancelled = true; } });
      f.mcpRun = { id, proposal, emit, finish() {
        proposal.status = proposal.status === 'rejected' ? 'rejected' : 'applied';
        emit({ type: 'approval_update', approval: proposal }); emit({ type: 'final', content: '公开 MCP 演示已完成' });
        emit({ type: 'done', stopped_reason: 'finished', session_saved: false, record_saved: true, usage_complete: true });
        closed = true; controller.close();
      } };
      return new Response(body, { headers: { 'Content-Type': 'text/event-stream', 'X-Run-Id': id } });
    }
    if (path.includes('/approvals/') && f.case === 'mcp') {
      f.mcpDecisions.push(payload.decision);
      f.mcpRun.proposal.status = payload.decision === 'approve' ? 'approved' : 'rejected';
      return json({ status: f.mcpRun.proposal.status });
    }
    const response = await original(input, options);
    if (path === '/api/tools' && f.mcp.enabled && f.mcp.servers.some(s => s.enabled)) {
      const tools = await response.json();
      tools.push({ name: 'mcp_public_exa_search', description: '公开搜索示例', source: 'mcp', server_name: 'Exa 联网', remote_name: 'web_search_exa', parameters: { type: 'object', properties: { query: { type: 'string' } } } });
      return json(tools);
    }
    return response;
  };
})();
