import assert from 'node:assert/strict'
import test from 'node:test'
import { describeStop, parseTokenBudget } from '../src/lib/runtime.ts'
import { applyEvent, emptyTurn } from '../src/lib/stream.ts'
import { streamAgentEvents } from '../src/lib/sse.ts'

test('停止唤醒挂起的流读取并取消底层流', async () => {
  let cancelled = false
  const response = new Response(new ReadableStream({ cancel() { cancelled = true } }))
  const controller = new AbortController()
  const source = streamAgentEvents(response, { signal: controller.signal })
  const waiting = source.next()
  controller.abort()
  await assert.rejects(waiting, { name: 'AbortError' })
  assert.equal(cancelled, true)
  assert.equal(response.body.locked, false)
})

for (const reason of ['timeout', 'token_budget']) {
  test(`${reason} 显示预算终止，已有部分答案保留`, () => {
    const partial = applyEvent(emptyTurn(), { type: 'final', content: '已获得的结论' })
    const failed = applyEvent(partial, { type: 'error', content: '预算耗尽' })
    const done = applyEvent(failed, { type: 'done', stopped_reason: reason, usage_complete: false,
      usage: { prompt_tokens: 10, completion_tokens: 3, total_tokens: 13 } })
    assert.equal(done.phase, 'done')
    assert.equal(done.answer, '已获得的结论')
    assert.equal(done.usageComplete, false)
    assert.equal(done.usage.total_tokens, 13)
    assert.equal(describeStop(reason).tone, 'warn')
    assert.match(describeStop(reason).text, /预算/)
  })
}

test('完整、缺失和旧版 Usage 契约有明确区别', () => {
  assert.equal(emptyTurn().usageComplete, false)
  const usage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }
  assert.equal(applyEvent(emptyTurn(), { type: 'done', usage, usage_complete: true }).usageComplete, true)
  assert.equal(applyEvent(emptyTurn(), { type: 'done', usage }).usageComplete, true)
  assert.equal(applyEvent(emptyTurn(), { type: 'done' }).usageComplete, false)
  assert.equal(describeStop('finished').tone, 'ok')
  assert.equal(describeStop('error').tone, 'danger')
})

test('阈值校验拒绝空值、负数、小数和不安全整数，0 可明确关闭', () => {
  for (const value of ['', '-1', '0.5', 'NaN', '1e3', '9007199254740992']) {
    assert.equal(parseTokenBudget(value), null, value)
  }
  assert.equal(parseTokenBudget('0'), 0)
  assert.equal(parseTokenBudget('60000'), 60000)
  assert.equal(parseTokenBudget(' 80000 '), 80000)
})
