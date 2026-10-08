/* Public synthetic desktop configuration. No actual model or filesystem access. */
(() => {
  const original = window.fetch.bind(window), f = window.__fixture;
  const json = (body, status = 200) => Response.json(body, { status });
  const d = f.desktop = { required: sessionStorage.getItem('smoke-first-setup') === 'true', setup: [],
    enabled: true, summary: true, model: null, facts: [], seq: 0, failConnect: false };
  d.restore = () => { window.fetch = original; };
  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href), path = url.pathname;
    const method = options.method || 'GET', payload = JSON.parse(options.body || '{}');
    if (path === '/api/chat/stream' && f.case === 'memory-confirmation') {
      const id = 'public-memory-' + (++d.seq);
      const proposal = { id, kind: 'memory', fact: '用户希望计划使用项目符号', tags: ['表达'], started: false, status: 'pending', message: '请核对希望长期保存的内容' };
      let controller, closed = false;
      const emit = event => { if (!closed) controller.enqueue(new TextEncoder().encode('event: ' + event.type + '\ndata: ' + JSON.stringify({ ...event, run_id: id }) + '\n\n')); };
      const body = new ReadableStream({ start(c) { controller = c; emit({ type: 'start' }); emit({ type: 'approval_request', approval: proposal }); }, cancel() { closed = true; d.cancelled = true; } });
      d.memoryRun = { id, proposal, emit, finish() {
        if (proposal.status === 'approved') { proposal.status = 'applied'; d.facts.push({ id, text: proposal.fact, tags: proposal.tags, ts: 1 }); }
        emit({ type: 'approval_update', approval: proposal }); emit({ type: 'final', content: '公开记忆确认演示完成' });
        emit({ type: 'done', stopped_reason: 'finished', session_saved: true, record_saved: true, usage_complete: true });
        closed = true; controller.close();
      } };
      return new Response(body, { headers: { 'Content-Type': 'text/event-stream', 'X-Run-Id': id } });
    }
    if (path.includes('/approvals/') && f.case === 'memory-confirmation') {
      d.memoryRun.proposal.status = payload.decision === 'approve' ? 'approved' : 'rejected';
      return json({ status: d.memoryRun.proposal.status });
    }
    if (path === '/api/setup') {
      if (method === 'POST') {
        d.setup.push(payload); d.required = false; d.model = payload.model;
        sessionStorage.removeItem('smoke-first-setup'); f.healthCase = 'ready';
      }
      return json({ required: d.required, desktop: true, stores_locally: true, session_persistent: true });
    }
    if (path === '/api/storage') return json({ data_directory: '公开合成用户数据目录 / Legacy', sessions: { backend: 'sqlite', persistent: true, ttl_seconds: 0 },
      memory: { active_backend: 'sql', active_enabled: d.enabled, enable_summary: d.summary }, runs: { active_backend: 'sql' } });
    if (path === '/api/memory') {
      if (method === 'POST') d.facts.push({ ...payload, id: 'memory-' + (++d.seq), ts: 1 });
      if (method === 'DELETE' && url.searchParams.get('confirm') === 'true') d.facts = [];
      return json({ enabled: d.enabled, backend: 'sql', max_facts: 200, facts: d.facts });
    }
    if (path.startsWith('/api/memory/')) {
      const id = path.split('/').at(-1);
      if (method === 'PUT') Object.assign(d.facts.find(v => v.id === id), payload);
      if (method === 'DELETE') d.facts = d.facts.filter(v => v.id !== id);
      return json({ saved: true });
    }
    if (path === '/api/settings' && method === 'PUT') {
      if ('memory_enabled' in payload) d.enabled = payload.memory_enabled;
      if ('memory_enable_summary' in payload) d.summary = payload.memory_enable_summary;
    }
    const response = await original(input, options);
    if (path === '/healthz' || path === '/api/meta') {
      const body = await response.json();
      if (d.required) body.llm_configured = false;
      if (d.model) body.model = d.model;
      return json(body);
    }
    return response;
  };
})();
