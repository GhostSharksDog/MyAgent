import assert from 'node:assert/strict'
import test from 'node:test'
import { readApproval, mergeApproval, closeApprovals, describeApproval } from '../src/lib/approvals.ts'
import { applyEvent, emptyTurn } from '../src/lib/stream.ts'
import { executionActivity } from '../src/lib/runtime.ts'
import { parseTerminalTimeout } from '../src/lib/terminal.ts'

const command = (extra = {}) => ({ id: 'command-1', kind: 'command',
  command: 'Write-Output "公开样本"\nWrite-Output "最后一行"', cwd: 'D:\\public-workspace',
  shell: 'powershell', timeout_seconds: 30, status: 'pending', message: '等待逐次确认', ...extra })
const file = (extra = {}) => ({ id: 'file-1', path: 'public.md', operation: 'create',
  diff: '+public\n', before_bytes: 0, after_bytes: 7, status: 'pending', message: '公开样本', ...extra })

test('命令保留完整多行文本、起始目录与时限，旧文件事件缺 kind 仍兼容', () => {
  const item = command({ command: '# public\n'.repeat(1000) + 'Write-Output "最后一行"' })
  assert.deepEqual(readApproval(item), item)
  const state = applyEvent(emptyTurn('thinking'), { type: 'approval_request', approval: item })
  assert.equal(state.approvals[0].command, item.command)
  assert.equal(state.approvals[0].cwd, item.cwd)
  assert.deepEqual(readApproval(file()), file())
  assert.deepEqual(readApproval(file({ kind: 'file' })), file({ kind: 'file' }))
  assert.equal(readApproval(file({ kind: 'unknown' })), null)
})

test('不完整命令不能产生可批准卡片，即使附带完整文件字段也不回退文件审批', () => {
  const bad = [
    { kind: undefined }, { kind: 'shell' }, { command: undefined }, { command: '' },
    { command: ' ' }, { command: 'echo\0public' }, { command: 'x'.repeat(16001) },
    { cwd: undefined }, { cwd: 'relative/path' }, { cwd: 'D:relative' }, { cwd: '\0' },
    { shell: undefined }, { shell: ' ' }, { timeout_seconds: undefined }, { timeout_seconds: '30' },
    { timeout_seconds: 0 }, { timeout_seconds: -1 }, { timeout_seconds: Infinity },
    { timeout_seconds: 600.1 }, { status: 'success' }, { message: undefined },
    { started: 'true' }, { started: true },
  ]
  for (const extra of bad) {
    const item = command(extra)
    assert.equal(readApproval(item), null, JSON.stringify(extra))
    assert.deepEqual(applyEvent(emptyTurn(), { type: 'approval_request', approval: item }).approvals, [])
  }
  assert.equal(readApproval({ ...file(), ...command({ command: '' }) }), null)
  assert.deepEqual(readApproval(command({ shell: 'sh', cwd: '/tmp/public' })),
    command({ shell: 'sh', cwd: '/tmp/public' }))
})

test('同一提案 ID 不得替换命令、目录、shell、时限或切换审批类型', () => {
  const items = mergeApproval([], command())
  for (const change of [command({ command: 'Write-Output changed' }), command({ cwd: 'D:\\other' }),
    command({ shell: 'sh' }), command({ timeout_seconds: 60 }), file({ id: 'command-1' })])
    assert.deepEqual(mergeApproval(items, change), items)
  const fileItems = mergeApproval([], file())
  assert.deepEqual(mergeApproval(fileItems, command({ id: 'file-1' })), fileItems)
  assert.deepEqual(mergeApproval(fileItems, file({ diff: '+unreviewed\n' })), fileItems)
})

test('批准命令保持执行中，执行失败与迟到帧不能变成成功', () => {
  let items = mergeApproval([], command())
  items = mergeApproval(items, command({ status: 'approved' }))
  assert.equal(describeApproval(items[0].status, 'command').tone, 'pending')
  assert.match(describeApproval(items[0].status, 'command').title, /等待执行/)
  items = mergeApproval(items, command({ status: 'approved', started: true }))
  assert.match(describeApproval(items[0].status, 'command', items[0].started).title, /正在执行/)
  items = mergeApproval(items, command({ status: 'approved' }))
  assert.equal(items[0].started, true, '迟到批准不能让已开始的命令退回等待')
  items = mergeApproval(items, command({ status: 'failed', message: '进程退出码 3，已有副作用不会撤销' }))
  assert.equal(describeApproval(items[0].status, 'command').tone, 'error')
  assert.deepEqual(mergeApproval(items, command({ status: 'applied' })), items)
  assert.deepEqual(mergeApproval(items, command()), items)
  assert.equal(describeApproval('applied', 'command').title, '命令执行完成')
})

test('运行进度区分文件、命令与同时等待，取消命令不宣称未产生副作用', () => {
  const state = applyEvent(emptyTurn('thinking'), { type: 'approval_request', approval: command() })
  assert.deepEqual(executionActivity(state), { text: '等待批准终端命令', progress: '1 项待确认' })
  const mixed = applyEvent(state, { type: 'approval_request', approval: file() })
  assert.deepEqual(executionActivity(mixed), { text: '等待批准工具操作', progress: '2 项待确认' })
  const closed = closeApprovals(mixed.approvals, '文件关闭提示')
  assert.equal(closed[0].status, 'cancelled')
  assert.match(closed[0].message, /副作用不会自动撤销/)
  assert.equal(closed[1].message, '文件关闭提示')
})

test('预算终止关闭已批准命令，失败终态仍保留并拒绝跨轮事件', () => {
  let state = applyEvent(emptyTurn('thinking'), { type: 'approval_request', run_id: 'public-run', approval: command() })
  state = applyEvent(state, { type: 'approval_update', approval: command({ status: 'approved' }) })
  assert.equal(applyEvent(state, { type: 'approval_update', run_id: 'other-run', approval: command({ status: 'applied' }) }), state)
  state = applyEvent(state, { type: 'done', stopped_reason: 'timeout' })
  assert.equal(state.approvals[0].status, 'cancelled')
  assert.equal(applyEvent(state, { type: 'approval_update', approval: command({ status: 'applied' }) }), state)
})

test('终端时限必须有限且为正，执行上限 600 秒与等待上限 3600 秒分别校验', () => {
  assert.equal(parseTerminalTimeout('1', 600), 1)
  assert.equal(parseTerminalTimeout(' 30.5 ', 600), 30.5)
  assert.equal(parseTerminalTimeout('600', 600), 600)
  assert.equal(parseTerminalTimeout('600.1', 600), null)
  assert.equal(parseTerminalTimeout('3600', 3600), 3600)
  assert.equal(parseTerminalTimeout('3600.1', 3600), null)
  for (const value of ['', ' ', '0', '.5', '-1', 'NaN', 'Infinity', '1e999']) {
    assert.equal(parseTerminalTimeout(value, 600), null)
    assert.equal(parseTerminalTimeout(value, 3600), null)
  }
})
