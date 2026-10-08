import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'
import { readApproval, mergeApproval, closeApprovals, describeApproval } from '../src/lib/approvals.ts'
import { emptyTurn, applyEvent } from '../src/lib/stream.ts'
import { runWarnings, executionActivity } from '../src/lib/runtime.ts'

const source = stripTypeScriptTypes(readFileSync(new URL('../src/lib/mcp.ts', import.meta.url), 'utf8'))
  .replace(/^import .*$/gm, '').replace(/^export /gm, '')
const makeClient = new Function('fetch', 'apiUrl', 'accessHeaders', source + '\nreturn { newMCPConfig, stringMap, mcpRequest }')
const client = makeClient(() => {}, x => x, () => ({}))
const proposal = (extra = {}) => ({ kind: 'mcp', id: 'public', server_id: 's', server_name: '公开服务',
  tool_name: 'write_public', fingerprint: 'a'.repeat(64), arguments: { text: '公开样本', nested: { b: 2, a: 1 } },
  started: false, status: 'pending', message: '等待批准', ...extra })

test('Exa 示例只预填公开地址和两个工具，默认禁用', () => {
  const config = client.newMCPConfig(true)
  assert.equal(config.enabled, false)
  assert.deepEqual(config.selected_tools, ['web_search_exa', 'web_fetch_exa'])
  assert.equal(new URL(config.url).hostname, 'mcp.exa.ai')
  assert.deepEqual(config.headers, {})
  assert.deepEqual(client.stringMap('{"Authorization":"********"}'), { Authorization: '********' })
  for (const text of ['[]', 'null', '{"x":2}', '{']) assert.throws(() => client.stringMap(text))
})

test('MCP 管理请求携带访问密钥、取消信号和结构化参数', async () => {
  let called
  const c = makeClient(async (...args) => { called = args; return Response.json({ enabled: false, servers: [] }) }, x => x, () => ({ 'X-API-Key': 'public-test' }))
  const controller = new AbortController()
  await c.mcpRequest('/servers', 'PUT', { name: 'public' }, controller.signal)
  assert.equal(called[0], '/api/mcp/servers')
  assert.equal(called[1].headers['X-API-Key'], 'public-test')
  assert.equal(called[1].signal, controller.signal)
  assert.deepEqual(JSON.parse(called[1].body), { name: 'public' })
  const bad = makeClient(async () => Response.json({ detail: '请先测试连接' }, { status: 409 }), x => x, () => ({}))
  await assert.rejects(bad.mcpRequest(), /请先测试连接/)
})

test('外部调用完整参数可审批，畸形提案不可审批', () => {
  assert.deepEqual(readApproval(proposal()), proposal())
  for (const extra of [{ fingerprint: '' }, { arguments: [] }, { server_id: '' }, { started: true }, { tool_name: null }])
    assert.equal(readApproval(proposal(extra)), null)
  const state = applyEvent(emptyTurn('thinking'), { type: 'approval_request', approval: proposal() })
  assert.equal(executionActivity(state).text, '等待批准外部工具')
})

test('同一提案不能替换参数或工具定义；JSON 字段顺序不影响身份', () => {
  const items = mergeApproval([], proposal())
  assert.equal(mergeApproval(items, proposal({ arguments: { text: 'different' } })), items)
  assert.equal(mergeApproval(items, proposal({ fingerprint: 'b'.repeat(64) })), items)
  const approved = mergeApproval(items, proposal({ arguments: { nested: { a: 1, b: 2 }, text: '公开样本' }, status: 'approved', started: true }))
  assert.equal(approved[0].started, true)
  assert.equal(mergeApproval(approved, proposal({ status: 'approved' }))[0].started, true)
  assert.equal(closeApprovals(approved, '结束')[0].status, 'cancelled')
  assert.match(closeApprovals(approved, '结束')[0].message, /未确认/)
  assert.equal(describeApproval('applied', 'mcp').title, '外部调用完成')
  assert.match(describeApproval('cancelled', 'mcp', true).title, /未确认/)
})

test('会话保存失败与运行摘要保存独立，不随统计隐藏', () => {
  const state = applyEvent(emptyTurn(), { type: 'done', stopped_reason: 'finished', usage_complete: true, session_saved: false, record_saved: true })
  assert.equal(state.sessionSaved, false)
  assert.equal(state.recordSaved, true)
  assert.deepEqual(runWarnings(state).map(w => w.title), ['会话未保存'])
})
