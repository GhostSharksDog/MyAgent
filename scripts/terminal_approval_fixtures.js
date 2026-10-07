/* Public terminal transport for Edge checks. No command or model is executed. */
(() => {
  const originalFetch = window.fetch.bind(window);
  const fixture = window.__fixture;
  const json = (body, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
  let sequence = 0;
  Object.assign(fixture.settings.agent, {
    terminal_enabled: fixture.settings.agent.terminal_enabled ?? false,
    terminal_timeout: fixture.settings.agent.terminal_timeout ?? 30,
    terminal_approval_timeout: fixture.settings.agent.terminal_approval_timeout ?? 300,
  });
  fixture.terminalDecisions = [];
  fixture.terminalCancelled = false;
  fixture.terminalRun = null;
  fixture.restoreTerminal = () => { window.fetch = originalFetch; };
  fixture.completeTerminal = status => fixture.terminalRun?.finish(status);
  fixture.startTerminal = () => fixture.terminalRun?.start();
  fixture.releaseTerminal = () => {
    const current = fixture.terminalRun;
    if (!current) return;
    if (current.proposal.status === 'approved') current.start();
    current.finish(current.proposal.status === 'rejected' ? 'rejected'
      : current.selected === 'terminal-failed' ? 'failed' : 'applied');
  };

  function stream(options) {
    const request = JSON.parse(options.body || '{}');
    const selected = fixture.case;
    const runId = 'public-terminal-run-' + (++sequence);
    const proposal = {
      id: 'public-terminal-' + sequence, kind: 'command',
      command: 'Write-Output "公开合成样本"\nWrite-Output "最后一行：完整命令可见"',
      cwd: 'C:/Legacy/demo', shell: 'powershell', timeout_seconds: 30,
      status: 'pending', started: false,
      message: '公开合成命令。批准前不执行，浏览器测试不会运行任何程序。',
    };
    const wireProposal = () => selected === 'terminal-malformed'
      ? { ...proposal, command: undefined } : { ...proposal };
    let controller;
    let closed = false;
    const emit = event => {
      if (!closed) controller.enqueue(new TextEncoder().encode(
        'event: ' + event.type + '\ndata: ' + JSON.stringify({ ...event, run_id: runId }) + '\n\n'));
    };
    const messages = {
      applied: '公开合成：退出码 0，命令执行完成。',
      rejected: '已拒绝此命令，没有执行。',
      expired: '确认等待超时，没有执行此命令。',
      failed: '公开合成：退出码 3。命令执行失败，已产生的副作用不会自动撤销。',
      conflict: '执行条件已变化，没有启动命令，请重新确认。',
      cancelled: '命令已取消，已产生的副作用不会自动撤销。',
    };
    const finish = (status = 'applied') => {
      if (closed) return;
      proposal.status = status;
      proposal.message = messages[status] || '公开合成终态。';
      emit({ type: 'approval_update', step: 1, approval: wireProposal() });
      const result = { exitcode: status === 'applied' ? 0 : status === 'failed' ? 3 : null,
        stdout: status === 'applied' ? '公开合成样本\n最后一行：完整命令可见\n' : '',
        stderr: status === 'failed' ? 'Public synthetic failure\n' : '', message: proposal.message };
      emit({ type: 'tool_result', step: 1, tool_name: 'run_terminal', tool_ok: status === 'applied',
        ...result, content: JSON.stringify(result), duration_ms: 8, truncated: false });
      if (request.mode === 'multi') emit({ type: 'delegate_result', specialist: '资料分析员',
        tool_ok: status === 'applied', content: proposal.message });
      if (request.mode === 'plan') emit({ type: 'plan_step', plan: {
        goal: '执行公开合成命令', reasoning: '先由用户确认，再记录结果。',
        steps: [{ id: 1, description: '确认并执行公开命令', status: status === 'applied' ? 'done' : 'failed', result: proposal.message }],
      } });
      emit({ type: 'final', step: 2, content: proposal.message });
      emit({ type: 'done', stopped_reason: 'finished', steps_used: 2, record_saved: false,
        usage_complete: true, usage: { prompt_tokens: 32, completion_tokens: 16, total_tokens: 48 } });
      closed = true;
      controller.close();
    };
    const start = () => {
      if (closed || proposal.status !== 'approved') return;
      proposal.started = true;
      proposal.message = '公开合成：命令已开始执行，等待进程结果。';
      emit({ type: 'approval_update', step: 1, approval: wireProposal() });
    };
    const current = { runId, id: proposal.id, proposal, selected, attempts: 0, finish, start,
      get controller() { return controller; } };
    fixture.terminalRun = current;
    fixture.terminalCancelled = false;
    const body = new ReadableStream({
      start(value) {
        controller = value;
        emit({ type: 'start', step: 0 });
        if (request.mode === 'plan') emit({ type: 'plan', plan: {
          goal: '执行公开合成命令', reasoning: '先由用户确认，再记录结果。',
          steps: [{ id: 1, description: '确认并执行公开命令', status: 'running' }],
        } });
        if (request.mode === 'multi') emit({ type: 'delegate', specialist: '资料分析员', content: '执行公开合成命令' });
        emit({ type: 'step', step: 1 });
        emit({ type: 'tool_call', step: 1, tool_name: 'run_terminal', tool_args: {
          command: proposal.command, cwd: proposal.cwd,
        } });
        emit({ type: 'approval_request', step: 1, approval: wireProposal() });
      },
      cancel() { closed = true; fixture.terminalCancelled = true; proposal.status = 'cancelled'; },
    });
    return new Response(body, { headers: { 'Content-Type': 'text/event-stream', 'X-Run-Id': runId } });
  }

  window.fetch = async (input, options = {}) => {
    const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
    if (path === '/api/chat/stream' && String(fixture.case).startsWith('terminal-')) return stream(options);
    const route = path.match(/^\/api\/runs\/([^/]+)\/approvals\/([^/]+)$/);
    if (route && String(fixture.case).startsWith('terminal-')) {
      const request = { path, method: options.method || 'GET', ...JSON.parse(options.body || '{}') };
      fixture.terminalDecisions.push(request);
      const current = fixture.terminalRun;
      if (!current || route[1] !== current.runId || route[2] !== current.id || current.proposal.status !== 'pending')
        return json({ detail: '本轮或命令确认已关闭，请重新发起任务。' }, 409);
      if (request.method !== 'POST' || !['approve', 'reject'].includes(request.decision))
        return json({ detail: 'Invalid public synthetic decision' }, 422);
      current.attempts++;
      current.proposal.status = request.decision === 'approve' ? 'approved' : 'rejected';
      return json({ status: current.proposal.status });
    }
    const response = await originalFetch(input, options);
    // The original fixture owns save/read generations. Extend only derived capability views.
    if (fixture.settings.agent.terminal_enabled && response.ok &&
      ['/api/tools', '/healthz', '/api/meta'].includes(path)) {
      const body = await response.json();
      if (path === '/api/tools') body.push({ name: 'run_terminal',
        description: '公开合成终端工具：每条命令需用户批准，不执行真实程序。',
        parameters: { type: 'object', properties: { command: { type: 'string' }, cwd: { type: 'string' } }, required: ['command'] } });
      if (path === '/healthz') body.tools.push('run_terminal');
      if (path === '/api/meta') body.tool_count++;
      return json(body);
    }
    return response;
  };
})();
