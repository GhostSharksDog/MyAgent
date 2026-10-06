import assert from 'node:assert/strict'
import test from 'node:test'
import { readApproval, mergeApproval, closeApprovals, describeApproval, parseApprovalTimeout,
  diffLineKind } from '../src/lib/approvals.ts'
import { applyEvent, emptyTurn } from '../src/lib/stream.ts'
import { executionActivity } from '../src/lib/runtime.ts'

const proposal = (extra = {}) => ({ id: 'p-1', path: 'public/notes.md', operation: 'edit',
  diff: '--- a/public/notes.md\n+++ b/public/notes.md\n@@ -1 +1 @@\n-old\n+new\n',
  before_bytes: 4, after_bytes: 4, before_format: 'UTF-8 · LF', after_format: 'UTF-8 · LF',
  status: 'pending', message: '公开样本', ...extra })

test('完整预览结构通过校验，长 diff 不被归约器截断', () => {
  const item = proposal({ diff: '+public sample\n'.repeat(20000) })
  assert.deepEqual(readApproval(item), item)
  const state = applyEvent(emptyTurn('thinking'), { type: 'approval_request', run_id: 'run-a', approval: item })
  assert.equal(state.approvals[0].diff, item.diff)
  assert.equal(state.approvals[0].before_format, 'UTF-8 · LF')
  assert.equal(state.approvals[0].busy, false)
})

for (const bad of [null, {}, proposal({ id: '' }), proposal({ status: 'success' }),
  proposal({ diff: null }), proposal({ before_bytes: -1 }), proposal({ after_bytes: Infinity }),
  proposal({ before_format: {} }), proposal({ operation: 'delete' })]) {
  test(`非法预览不能产生可批准状态：${JSON.stringify(bad)}`, () => {
    assert.equal(readApproval(bad), null)
    assert.deepEqual(applyEvent(emptyTurn(), { type: 'approval_request', approval: bad }).approvals, [])
  })
}

test('重复 request 按提案 ID 更新，两个同路径修改仍分别确认', () => {
  let state = applyEvent(emptyTurn('thinking'), { type: 'approval_request', approval: proposal() })
  state = applyEvent(state, { type: 'approval_request', approval: proposal() })
  state = applyEvent(state, { type: 'approval_request', approval: proposal({ id: 'p-2' }) })
  assert.equal(state.approvals.length, 2)
  assert.deepEqual(executionActivity(state), { text: '等待批准文件修改', progress: '2 项待确认' })
})

test('重复 pending 帧不能释放仍在途的决定锁或抹去错误提示', () => {
  const items = [{ ...proposal(), busy: true, error: '连接失败，请重试' }]
  const updated = mergeApproval(items, proposal())
  assert.equal(updated[0].busy, true)
  assert.equal(updated[0].error, '连接失败，请重试')
})

test('批准只进入核验，applied 才能显示成功；迟到 request 不回退状态', () => {
  let items = mergeApproval([], proposal())
  items = mergeApproval(items, proposal({ status: 'approved' }))
  assert.equal(describeApproval(items[0].status).title, '已批准 · 正在核验')
  assert.equal(describeApproval(items[0].status).tone, 'pending')
  items = mergeApproval(items, proposal())
  assert.equal(items[0].status, 'approved')
  items = mergeApproval(items, proposal({ status: 'applied' }))
  items = mergeApproval(items, proposal({ status: 'approved' }))
  assert.equal(items[0].status, 'applied')
  assert.equal(describeApproval(items[0].status).title, '文件已写入')
})

for (const status of ['rejected', 'expired', 'cancelled', 'conflict', 'failed']) {
  test(`${status} 保留明确终态，迟到批准和等待帧不能复活`, () => {
    const items = mergeApproval([], proposal({ status }))
    assert.deepEqual(mergeApproval(items, proposal({ status: 'approved' })), items)
    assert.deepEqual(mergeApproval(items, proposal()), items)
    assert.notEqual(describeApproval(status).tone, 'ok')
  })
}

test('预算结束关闭未决确认但保留已应用和已拒绝的证据', () => {
  let state = emptyTurn('thinking')
  for (const [id, status] of [['wait', 'pending'], ['check', 'approved'], ['done', 'applied'], ['no', 'rejected']])
    state = applyEvent(state, { type: 'approval_update', approval: proposal({ id, status }) })
  state = applyEvent(state, { type: 'done', stopped_reason: 'timeout', usage_complete: true })
  assert.deepEqual(state.approvals.map((item) => item.status), ['cancelled', 'cancelled', 'applied', 'rejected'])
  assert.match(state.approvals[1].message, /未收到写入成功/)
  assert.equal(applyEvent(state, { type: 'approval_request', approval: proposal() }), state)
})

test('取消关闭本轮确认，其他 run 的帧不能改变当前 runId 或追加提案', () => {
  const state = applyEvent(emptyTurn('thinking'), { type: 'approval_request', run_id: 'run-a', approval: proposal() })
  assert.equal(applyEvent(state, { type: 'approval_request', run_id: 'run-b', approval: proposal({ id: 'foreign' }) }), state)
  const closed = { ...state, phase: 'aborted', approvals: closeApprovals(state.approvals, '已停止') }
  assert.equal(applyEvent(closed, { type: 'approval_update', approval: proposal({ status: 'applied' }) }), closed)
  assert.equal(closed.approvals[0].status, 'cancelled')
})

test('确认时限允许 0 和小数秒，拒绝空白、负数、非数与无穷大', () => {
  assert.equal(parseApprovalTimeout('0'), 0)
  assert.equal(parseApprovalTimeout('0.125'), .125)
  assert.equal(parseApprovalTimeout(' 300 '), 300)
  for (const value of ['', ' ', '-1', 'abc', 'NaN', 'Infinity', '1e999']) assert.equal(parseApprovalTimeout(value), null)
})

test('diff 文件头不是删除行，新增、删除和上下文分别着色', () => {
  assert.equal(diffLineKind('--- a/notes.md'), 'header')
  assert.equal(diffLineKind('+++ b/notes.md'), 'header')
  assert.equal(diffLineKind('@@ -1 +1 @@'), 'header')
  assert.equal(diffLineKind('-before'), 'remove')
  assert.equal(diffLineKind('+after'), 'add')
  assert.equal(diffLineKind(' unchanged'), 'context')
})
