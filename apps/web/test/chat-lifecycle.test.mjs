import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

import { streamAgentEvents } from '../src/lib/sse.ts'
import { applyEvent, emptyTurn } from '../src/lib/stream.ts'
import { closeApprovals } from '../src/lib/approvals.ts'

// 执行真实 Hook 源码，只替换 React 的状态容器和网络入口。
// 取消底层流的延迟保留为真实 ReadableStream 行为，不能让旧轮次立刻消失。
const source = stripTypeScriptTypes(readFileSync(new URL('../src/hooks/useChat.ts', import.meta.url), 'utf8'))
  .replace(/^import[\s\S]*?from\s+['"][^'"]+['"]\s*$/gm, '')
  .replace(/^export /gm, '')
const makeHook = new Function('useState', 'useRef', 'useCallback', 'useEffect',
  'openChatStream', 'streamAgentEvents', 'applyEvent', 'emptyTurn', 'ApiError', 'closeApprovals',
  'decideFileApproval', source + '\nreturn useChat;')

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function harness(open, decide = async () => ({ status: 'approved' })) {
  const slots = []
  const cleanups = []
  let cursor = 0
  let updates = 0
  const useState = (initial) => {
    const slot = cursor++
    if (!(slot in slots)) slots[slot] = initial
    return [slots[slot], (value) => {
      updates++
      slots[slot] = typeof value === 'function' ? value(slots[slot]) : value
    }]
  }
  const useRef = (initial) => {
    const slot = cursor++
    if (!(slot in slots)) slots[slot] = { current: initial }
    return slots[slot]
  }
  const useEffect = (effect) => {
    const slot = cursor++
    if (!(slot in slots)) {
      slots[slot] = true
      const cleanup = effect()
      if (typeof cleanup === 'function') cleanups.push(cleanup)
    }
  }
  const useChat = makeHook(useState, useRef, (callback) => callback, useEffect,
    open, streamAgentEvents, applyEvent, emptyTurn, class ApiError extends Error {}, closeApprovals, decide)
  let settled = 0
  const render = (options = {}) => {
    cursor = 0
    return useChat({ onTurnSettled: () => { settled++ }, ...options })
  }
  return { render, unmount: () => cleanups.forEach((cleanup) => cleanup()),
    get settled() { return settled }, get updates() { return updates } }
}

const tick = () => new Promise((resolve) => setImmediate(resolve))

test('SSE 首帧前取消也保留响应头里的运行标识', async () => {
  let closed = false
  const response = new Response(new ReadableStream({ cancel() { closed = true } }),
    { headers: { 'Content-Type': 'text/event-stream', 'X-Run-Id': 'run-before-first-frame' } })
  const h = harness(async () => response)
  const pending = h.render().send('公开任务', null, 'react')
  await tick()
  assert.equal(h.render().items.at(-1).state.runId, 'run-before-first-frame')
  h.render().abort()
  await pending
  assert.equal(closed, true)
  assert.equal(h.render().items.at(-1).state.runId, 'run-before-first-frame')
  assert.equal(h.render().items.at(-1).state.phase, 'aborted')
})

for (const change of ['loadHistory', 'clear']) {
  test(`${change} 后旧流迟到清理不能抹掉新流的状态与停止控制器`, async () => {
    const delayedCleanup = deferred()
    const requests = []
    const view = harness(async (body, signal) => {
      const index = requests.length
      requests.push({ body, signal })
      return new Response(new ReadableStream({
        cancel: () => index === 0 ? delayedCleanup.promise : undefined,
      }))
    })
    const old = view.render().send('上一轮', 'A')
    await tick() // 旧流已在挂起的 read 中。
    const chat = view.render()
    if (change === 'loadHistory') chat.loadHistory([])
    else chat.clear()
    assert.equal(view.render().isStreaming, false)
    const current = view.render().send('新一轮', 'B')
    await tick()
    assert.equal(requests[0].signal.aborted, true)
    assert.equal(requests[1].signal.aborted, false)

    delayedCleanup.resolve()
    await old
    assert.equal(view.render().isStreaming, true, '新轮次仍在等待数据')
    assert.equal(view.render().notice, null, '旧取消不能给新轮次加停止提示')
    assert.equal(view.settled, 0, '旧轮次退出不能刷新当前会话')
    await view.render().send('不应重复发送', 'B')
    assert.equal(requests.length, 2, '不能因旧清理重置锁而开启并行发送')

    view.render().abort()
    assert.equal(requests[1].signal.aborted, true, '停止仍能取消新控制器')
    await current
    assert.equal(view.render().isStreaming, false)
    assert.equal(view.render().items.at(-1).state.phase, 'aborted')
    assert.equal(view.settled, 1)
  })
}

test('旧 fetch 迟到返回时关闭其响应，不能覆盖新轮次；当前轮仍正常结束', async () => {
  const requests = []
  const view = harness((body, signal) => {
    const result = deferred()
    requests.push({ body, signal, ...result })
    return result.promise
  })
  const old = view.render().send('旧请求', 'A')
  view.render().loadHistory([])
  const current = view.render().send('新请求', 'B')
  let cancelled = 0
  requests[0].resolve(new Response(new ReadableStream({ cancel: () => { cancelled++ } })))
  await old
  assert.equal(cancelled, 1)
  assert.equal(view.render().isStreaming, true)
  assert.equal(view.render().items.length, 2)
  assert.equal(view.render().notice, null)

  const events = [
    { type: 'final', step: 1, content: '新请求的答案' },
    { type: 'done', stopped_reason: 'finished', usage_complete: true },
  ]
  const text = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('')
  requests[1].resolve(new Response(text))
  await current
  assert.equal(view.render().items.at(-1).state.answer, '新请求的答案')
  assert.equal(view.render().isStreaming, false)
  assert.equal(view.settled, 1)
})

test('旧请求迟到异常不污染当前轮；卸载后不更新状态或刷新会话', async () => {
  const requests = []
  const view = harness((body, signal) => {
    const result = deferred()
    requests.push({ body, signal, ...result })
    return result.promise
  })
  const old = view.render().send('旧请求', 'A')
  view.render().clear()
  const current = view.render().send('当前请求', 'B')
  requests[0].reject(new Error('迟到的连接错误'))
  await old
  assert.equal(view.render().notice, null)
  assert.equal(view.render().items.at(-1).state.error, null)
  assert.equal(view.render().isStreaming, true)

  const count = view.updates
  view.unmount()
  assert.equal(requests[1].signal.aborted, true)
  requests[1].reject(new DOMException('cancelled', 'AbortError'))
  await current
  assert.equal(view.updates, count, '卸载后的异常不能 setState')
  assert.equal(view.settled, 0)
})

const approval = (extra = {}) => ({ id: 'proposal-1', path: 'public.txt', operation: 'create',
  diff: '--- /dev/null\n+++ b/public.txt\n@@ -0,0 +1 @@\n+公开样本\n',
  before_bytes: 0, after_bytes: 12, status: 'pending', message: '请确认', ...extra })

function controlledChat(decide) {
  const streams = []
  const h = harness(async () => {
    let streamController
    const body = new ReadableStream({ start(controller) { streamController = controller } })
    streams.push({ push(event) { streamController.enqueue(new TextEncoder().encode(
      `data: ${JSON.stringify(event)}\n\n`)) }, close() { streamController.close() } })
    return new Response(body, { headers: { 'X-Run-Id': `run-${streams.length}` } })
  }, decide)
  return { h, streams }
}

test('确认同步锁挡住同帧双击，POST 成功只显示 approved；applied 由 SSE 确认', async () => {
  const request = deferred()
  const calls = []
  const { h, streams } = controlledChat((...args) => { calls.push(args); return request.promise })
  const sending = h.render().send('建立公开文件', null)
  await tick()
  streams[0].push({ type: 'approval_request', approval: approval() })
  await tick()
  const chat = h.render()
  const id = chat.activeAssistantId
  const deciding = chat.decideApproval(id, 'proposal-1', 'approve')
  await chat.decideApproval(id, 'proposal-1', 'approve')
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0].slice(0, 3), ['run-1', 'proposal-1', 'approve'])
  assert.equal(h.render().items.at(-1).state.approvals[0].busy, true)
  request.resolve({ status: 'approved' })
  await deciding
  assert.equal(h.render().items.at(-1).state.approvals[0].status, 'approved')
  assert.match(h.render().items.at(-1).state.approvals[0].message, /才算完成/)
  streams[0].push({ type: 'approval_update', approval: approval({ status: 'applied', message: '写入完成' }) })
  streams[0].push({ type: 'done', stopped_reason: 'finished' })
  streams[0].close()
  await sending
  assert.equal(h.render().items.at(-1).state.approvals[0].status, 'applied')
  await h.render().decideApproval(id, 'proposal-1', 'approve')
  assert.equal(calls.length, 1, 'done 后旧确认不能发请求')
})

test('写入 SSE 先于 HTTP 返回时，迟到 approved 不覆盖 applied', async () => {
  const request = deferred()
  const { h, streams } = controlledChat(() => request.promise)
  const sending = h.render().send('建立公开文件', null)
  await tick()
  streams[0].push({ type: 'approval_request', approval: approval() })
  await tick()
  const deciding = h.render().decideApproval(h.render().activeAssistantId, 'proposal-1', 'approve')
  streams[0].push({ type: 'approval_update', approval: approval({ status: 'applied' }) })
  await tick()
  request.resolve({ status: 'approved' })
  await deciding
  assert.equal(h.render().items.at(-1).state.approvals[0].status, 'applied')
  h.render().abort()
  await sending
})

test('确认失败保留后端可操作错误，释放 busy 后可重试拒绝', async () => {
  const calls = []
  const { h, streams } = controlledChat((...args) => {
    const request = deferred(); calls.push({ args, ...request }); return request.promise
  })
  const sending = h.render().send('公开任务', null)
  await tick()
  streams[0].push({ type: 'approval_request', approval: approval() })
  await tick()
  const id = h.render().activeAssistantId
  const first = h.render().decideApproval(id, 'proposal-1', 'approve')
  calls[0].reject(new Error('访问密钥无效，请重新填写'))
  await first
  assert.equal(h.render().items.at(-1).state.approvals[0].busy, false)
  assert.match(h.render().items.at(-1).state.approvals[0].error, /重新填写/)
  const retry = h.render().decideApproval(id, 'proposal-1', 'reject')
  assert.equal(calls[1].args[2], 'reject')
  calls[1].resolve({ status: 'rejected' })
  await retry
  assert.equal(h.render().items.at(-1).state.approvals[0].status, 'rejected')
  h.render().abort()
  await sending
})

for (const change of ['abort', 'clear', 'loadHistory']) {
  test(`${change} 立即关闭确认并取消其网络；迟到结果不能影响新轮`, async () => {
    const request = deferred()
    const calls = []
    const { h, streams } = controlledChat((...args) => { calls.push(args); return request.promise })
    const sending = h.render().send('公开旧任务', null)
    await tick()
    streams[0].push({ type: 'approval_request', approval: approval() })
    await tick()
    const id = h.render().activeAssistantId
    const deciding = h.render().decideApproval(id, 'proposal-1', 'approve')
    if (change === 'loadHistory') h.render().loadHistory([])
    else h.render()[change]()
    assert.equal(calls[0][3].aborted, true)
    if (change === 'abort') assert.equal(h.render().items.at(-1).state.approvals[0].status, 'cancelled')
    await h.render().decideApproval(id, 'proposal-1', 'approve')
    assert.equal(calls.length, 1)
    await sending
    const current = h.render().send('公开新任务', null)
    await tick()
    request.resolve({ status: 'approved' })
    await deciding
    assert.deepEqual(h.render().items.at(-1).state.approvals, [])
    assert.equal(h.render().isStreaming, true)
    h.render().abort()
    await current
  })
}

test('未决确认遇到预算 done 后关闭，后续点击不能发 POST', async () => {
  let decisions = 0
  const { h, streams } = controlledChat(async () => { decisions++; return { status: 'approved' } })
  const sending = h.render().send('公开任务', null)
  await tick()
  streams[0].push({ type: 'approval_request', approval: approval() })
  await tick()
  const id = h.render().activeAssistantId
  streams[0].push({ type: 'done', stopped_reason: 'timeout' })
  streams[0].close()
  await sending
  assert.equal(h.render().items.at(-1).state.approvals[0].status, 'cancelled')
  await h.render().decideApproval(id, 'proposal-1', 'approve')
  assert.equal(decisions, 0)
})

test('只能决定活跃助手消息中的已收到提案，不能猜测旧消息或提案 ID', async () => {
  let calls = 0
  const { h, streams } = controlledChat(async () => { calls++; return { status: 'approved' } })
  const sending = h.render().send('公开任务', null)
  await tick()
  streams[0].push({ type: 'approval_request', approval: approval() })
  await tick()
  await h.render().decideApproval('history-1', 'proposal-1', 'approve')
  await h.render().decideApproval(h.render().activeAssistantId, 'unknown', 'approve')
  assert.equal(calls, 0)
  h.render().abort()
  await sending
})

test('卸载后在途确认响应不更新状态，当前 SSE 也被取消', async () => {
  const request = deferred()
  let signal
  const { h, streams } = controlledChat((...args) => { signal = args[3]; return request.promise })
  const sending = h.render().send('公开任务', null)
  await tick()
  streams[0].push({ type: 'approval_request', approval: approval() })
  await tick()
  const deciding = h.render().decideApproval(h.render().activeAssistantId, 'proposal-1', 'approve')
  h.unmount()
  const updates = h.updates
  assert.equal(signal.aborted, true)
  request.resolve({ status: 'approved' })
  await deciding
  await sending
  assert.equal(h.updates, updates)
})

test('切换会话读取期间真实 Hook 暂停审批，失败返回原会话后可继续决定', async () => {
  let calls = 0
  const { h, streams } = controlledChat(async () => { calls++; return { status: 'rejected' } })
  const sending = h.render().send('公开任务', null)
  await tick()
  streams[0].push({ type: 'approval_request', approval: approval() })
  await tick()
  const previousAction = h.render().decideApproval
  const id = h.render().activeAssistantId
  h.render({ approvalDisabled: true })
  await previousAction(id, 'proposal-1', 'approve')
  assert.equal(calls, 0, '旧渲染保存的回调也必须读取当前切换标记')
  assert.equal(h.render({ approvalDisabled: true }).items.at(-1).state.approvals[0].status, 'pending')
  await h.render({ approvalDisabled: false }).decideApproval(id, 'proposal-1', 'reject')
  assert.equal(calls, 1)
  h.render().abort()
  await sending
})

test('done 后连接尚未收尾时点停止，不覆盖权威终态和已写入记录', async () => {
  const { h, streams } = controlledChat(async () => ({ status: 'approved' }))
  const sending = h.render().send('公开任务', null)
  await tick()
  streams[0].push({ type: 'approval_update', approval: approval({ status: 'applied' }) })
  streams[0].push({ type: 'done', stopped_reason: 'finished' })
  await tick()
  h.render().abort()
  await sending
  assert.equal(h.render().items.at(-1).state.phase, 'done')
  assert.equal(h.render().items.at(-1).state.stoppedReason, 'finished')
  assert.equal(h.render().items.at(-1).state.approvals[0].status, 'applied')
  assert.equal(h.render().notice, null)
})
