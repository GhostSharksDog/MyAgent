import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import { readApproval, mergeApproval, describeApproval } from '../src/lib/approvals.ts'
import { emptyTurn } from '../src/lib/stream.ts'
import { executionActivity } from '../src/lib/runtime.ts'

const load = path => stripTypeScriptTypes(readFileSync(new URL(path, import.meta.url), 'utf8')).replace(/^import .*$/gm, '').replace(/^export /gm, '')
const draft = new Function(load('../src/lib/mcp.ts') + '\n' + load('../src/lib/mcp-draft.ts') + '\nreturn { presetDraft, detectPreset, applySecret }')()

test('五种 MCP 表单的公开预设，不含个人路径或密钥', () => {
  for (const preset of ['tavily', 'exa', 'filesystem', 'desktop_commander', 'custom']) {
    const config = draft.presetDraft(preset)
    assert.equal(config.preset, preset)
    assert.equal(config.enabled, false)
    assert.equal(config.cwd, '')
    assert.equal(config.command, '')
    assert.deepEqual(config.headers, {})
  }
  assert.deepEqual(draft.presetDraft('tavily').selected_tools, ['tavily-search', 'tavily-extract'])
})

test('旧 MCP 配置仍可识别，留空保留密钥，清除必须明确', () => {
  assert.equal(draft.detectPreset({ url: 'https://mcp.tavily.com/mcp/', args: [] }), 'tavily')
  assert.equal(draft.detectPreset({ url: '', args: ['D:\\bundle\\server-filesystem\\dist\\index.js'] }), 'filesystem')
  const masked = { Authorization: '********', 'X-Public': 'value' }
  assert.deepEqual(draft.applySecret(masked, '', 'bearer', '', false), masked)
  assert.deepEqual(draft.applySecret(masked, '', 'bearer', '', true), { 'X-Public': 'value' })
  assert.equal(draft.applySecret({}, 'example-key', 'bearer', '', false).Authorization, 'Bearer example-key')
  assert.equal(draft.applySecret({}, 'example-key', 'header', 'X-API-Key', false)['X-API-Key'], 'example-key')
  assert.throws(() => draft.applySecret({}, 'key', 'header', 'invalid header', false))
  assert.deepEqual(draft.applySecret(masked, '', 'none', 'X-API-Key', false), masked)
})

test('记忆确认完整校验，同 ID 不得偷换内容或类型', () => {
  const proposal = { id: 'm', kind: 'memory', fact: '用户偏好简短中文', tags: ['语言'], started: false, status: 'pending', message: '等待确认' }
  assert.deepEqual(readApproval(proposal), proposal)
  assert.equal(readApproval({ ...proposal, fact: '' }), null)
  assert.equal(readApproval({ ...proposal, tags: [1] }), null)
  assert.equal(readApproval({ ...proposal, started: true }), null)
  const items = mergeApproval([], proposal)
  assert.deepEqual(mergeApproval(items, { ...proposal, fact: '被替换的内容' }), items)
  assert.equal(describeApproval('applied', 'memory').title, '记忆已保存')
  const started = mergeApproval(items, { ...proposal, status: 'approved', started: true })
  assert.equal(mergeApproval(started, { ...proposal, status: 'approved' })[0].started, true)
  const state = emptyTurn('thinking')
  state.approvals = items
  assert.equal(executionActivity(state).text, '等待确认保存记忆')
})
