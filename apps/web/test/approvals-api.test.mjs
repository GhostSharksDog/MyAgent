import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

const source = stripTypeScriptTypes(readFileSync(new URL('../src/lib/api.ts', import.meta.url), 'utf8'))
  .replace(/^import[\s\S]*?from\s+['"][^'"]+['"]\s*$/gm, '')
  .replace(/import\.meta\.env\.VITE_API_BASE/g, "''")
  .replace(/^export /gm, '')
const makeClient = new Function('fetch', 'accessHeaders', source + '\nreturn { decideFileApproval, ApiError }')

test('审批 HTTP 编码 run 和 proposal 标识，用 JSON 决定与当前访问密钥头', async () => {
  let called
  const client = makeClient(async (...args) => {
    called = args
    return Response.json({ status: 'approved' })
  }, () => ({ 'X-API-Key': 'synthetic-access-key' }))
  const controller = new AbortController()
  const result = await client.decideFileApproval('run/a', 'proposal?x=1', 'approve', controller.signal)
  assert.equal(called[0], '/api/runs/run%2Fa/approvals/proposal%3Fx%3D1')
  assert.equal(called[1].method, 'POST')
  assert.equal(called[1].headers['X-API-Key'], 'synthetic-access-key')
  assert.equal(called[1].headers['Content-Type'], 'application/json')
  assert.equal(called[1].signal, controller.signal)
  assert.deepEqual(JSON.parse(called[1].body), { decision: 'approve' })
  assert.deepEqual(result, { status: 'approved' })
})

test('审批拒绝响应携带 actionable 后端错误，不把错误当作批准', async () => {
  const client = makeClient(async () => Response.json({ detail: '确认已失效，请重新发起任务。' },
    { status: 409 }), () => ({}))
  await assert.rejects(client.decideFileApproval('run', 'proposal', 'reject'),
    (error) => error instanceof client.ApiError && error.status === 409 && /重新发起任务/.test(error.message))
})

test('审批 fetch 被本轮取消时保留取消异常，不改写成离线错误', async () => {
  const controller = new AbortController()
  controller.abort()
  const failure = new DOMException('synthetic cancellation', 'AbortError')
  const client = makeClient(async () => { throw failure }, () => ({}))
  await assert.rejects(client.decideFileApproval('run', 'proposal', 'approve', controller.signal),
    (error) => error === failure)
})

test('HTTP 200 的非法状态不被当作批准或写入成功', async () => {
  const client = makeClient(async () => Response.json({ status: 'applied' }), () => ({}))
  await assert.rejects(client.decideFileApproval('run', 'proposal', 'approve'),
    (error) => error instanceof client.ApiError && /不能据此认定写入成功/.test(error.message))
})
