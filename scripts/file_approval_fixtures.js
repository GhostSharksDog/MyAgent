/* Public approval transport for Edge checks. Only approval-* conversations are intercepted. */
(() => {
  const originalFetch = window.fetch.bind(window);
  const fixture = window.__fixture;
  let sequence = 0;
  const json = (body, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
  fixture.approvalDecisions = [];
  fixture.approvalCancelled = false;
  fixture.approvalRun = null;
  fixture.restoreApproval = () => { window.fetch = originalFetch; };
  fixture.releaseApproval = () => fixture.approvalRun?.release();
  fixture.finishApproval = status => fixture.approvalRun?.finish(status);

  function stream(options) {
    const selected = fixture.case;
    const runId = 'public-approval-run-' + (++sequence);
    const id = 'public-approval-' + sequence;
    const operation = selected === 'approval-approve' ? 'create'
      : selected === 'approval-reject' ? 'overwrite' : 'edit';
    const before = '# Public project note\n\nStatus: draft\nOwner: user\nNext: confirm scope\n';
    const after = '# Public project note\n\nStatus: approved\nOwner: user\nNext: record evidence\n尾行：完整修改可见。\n';
    const proposal = {
      id, path: 'notes/public-action-plan.md', operation,
      diff: '--- a/notes/public-action-plan.md\n+++ b/notes/public-action-plan.md\n@@ -1,5 +1,6 @@\n # Public project note\n \n-Status: draft\n+Status: approved\n Owner: user\n-Next: confirm scope\n+Next: record evidence\n+尾行：完整修改可见。\n',
      before_bytes: operation === 'create' ? 0 : new TextEncoder().encode(before).length,
      after_bytes: new TextEncoder().encode(after).length,
      status: 'pending', message: '公开合成预览。批准后重新核验；浏览器测试不会写入文件。',
      before_format: operation === 'create' ? '目标不存在' : 'UTF-8，无 BOM，LF 换行',
      after_format: 'UTF-8，无 BOM，LF 换行',
    };
    if (operation === 'create') proposal.diff = '--- /dev/null\n+++ b/notes/public-action-plan.md\n@@ -0,0 +1,6 @@\n' + after.trimEnd().split('\n').map(line => '+' + line).join('\n') + '\n';
    let controller;
    let closed = false;
    const emit = event => {
      if (!closed) controller.enqueue(new TextEncoder().encode(
        'event: ' + event.type + '\ndata: ' + JSON.stringify(event) + '\n\n'));
    };
    const messages = {
      applied: '已重新核验文件与权限，公开合成修改成功。',
      rejected: '用户拒绝此修改，目标文件未改变。',
      conflict: '预览后文件已变化，本次修改未应用；请重新读取并生成新预览。',
      expired: '等待确认超时，本次修改未应用。',
      failed: '写入失败，本次修改未应用；请检查工作区。',
      cancelled: '本轮已取消，不能再批准此修改。',
    };
    const finish = status => {
      if (closed) return;
      proposal.status = status;
      proposal.message = messages[status] || '公开合成终态。';
      emit({ type: 'approval_update', step: 1, approval: { ...proposal } });
      emit({ type: 'tool_result', step: 1, tool_name: 'write_file', tool_ok: status === 'applied', content: proposal.message });
      emit({ type: 'final', step: 2, content: status === 'applied' ? '已完成公开样本修改。' : '本次修改未应用，已有内容保持不变。' });
      emit({ type: 'done', stopped_reason: 'finished', steps_used: 2, record_saved: false,
        usage_complete: true, usage: { prompt_tokens: 32, completion_tokens: 16, total_tokens: 48 } });
      closed = true;
      controller.close();
    };
    fixture.approvalRun = { runId, id, proposal, selected, attempts: 0, finish,
      release: () => finish(proposal.status === 'rejected' ? 'rejected'
        : selected === 'approval-conflict' ? 'conflict' : 'applied'),
    };
    fixture.approvalCancelled = false;
    const body = new ReadableStream({
      start(value) {
        controller = value;
        // run_id deliberately lives only in the response header, exercising the fallback.
        emit({ type: 'start', step: 0 });
        emit({ type: 'step', step: 1 });
        emit({ type: 'tool_call', step: 1, tool_name: 'write_file', tool_args: { path: proposal.path } });
        emit({ type: 'approval_request', step: 1, approval: { ...proposal } });
      },
      cancel() { closed = true; fixture.approvalCancelled = true; proposal.status = 'cancelled'; },
    });
    return new Response(body, { headers: { 'Content-Type': 'text/event-stream', 'X-Run-Id': runId } });
  }
  window.fetch = async (input, options = {}) => {
    const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
    if (path === '/api/chat/stream' && String(fixture.case).startsWith('approval-')) return stream(options);
    const route = path.match(/^\/api\/runs\/([^/]+)\/approvals\/([^/]+)$/);
    if (!route) return originalFetch(input, options);
    const request = { path, method: options.method || 'GET', ...JSON.parse(options.body || '{}') };
    fixture.approvalDecisions.push(request);
    const current = fixture.approvalRun;
    if (!current || route[1] !== current.runId || route[2] !== current.id || current.proposal.status !== 'pending') {
      return json({ detail: '本轮或确认已关闭，请重新发起任务。' }, 409);
    }
    if (request.method !== 'POST' || !['approve', 'reject'].includes(request.decision)) return json({ detail: 'Invalid synthetic decision' }, 422);
    current.attempts++;
    if (current.selected === 'approval-retry' && current.attempts === 1) {
      return json({ detail: '公开合成：确认请求暂时失败，请重试。' }, 503);
    }
    current.proposal.status = request.decision === 'approve' ? 'approved' : 'rejected';
    // Do not send applied here: the UI must distinguish a decision from a completed write.
    return json({ status: current.proposal.status });
  };
})();
