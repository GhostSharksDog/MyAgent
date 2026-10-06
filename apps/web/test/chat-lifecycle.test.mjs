import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

import { streamAgentEvents } from '../src/lib/sse.ts'
import { applyEvent, emptyTurn } from '../src/lib/stream.ts'

// 执行真实 Hook 源码，只替换 React 的状态容器和网络入口。
// 取消底层流的延迟保留为真实 ReadableStream 行为，不能让旧轮次立刻消失。
const source = stripTypeScriptTypes(readFileSync(new URL('../src/hooks/useChat.ts', import.meta.url), 'utf8'))
  .replace(/^import[\s\S]*?from\s+['"][^'"]+['"]\s*$/gm, '')
  .replace(/^export /gm, '')
const makeHook = new Function('useState', 'useRef', 'useCallback', 'useEffect',
  'openChatStream', 'streamAgentEvents', 'applyEvent', 'emptyTurn', 'ApiError', source + '\nreturn useChat;')

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function harness(open) {
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
    open, streamAgentEvents, applyEvent, emptyTurn, class ApiError extends Error {})
  let settled = 0
  const render = () => {
    cursor = 0
    return useChat({ onTurnSettled: () => { settled++ } })
  }
  return { render, unmount: () => cleanups.forEach((cleanup) => cleanup()),
    get settled() { return settled }, get updates() { return updates } }
}

const tick = () => new Promise((resolve) => setImmediate(resolve))

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
