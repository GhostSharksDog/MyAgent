import assert from 'node:assert/strict'
import test from 'node:test'
import { executionActivity, runWarnings } from '../src/lib/runtime.ts'
import { applyEvent, emptyTurn } from '../src/lib/stream.ts'

for (const reason of ['token_budget', 'timeout', 'max_steps']) {
  test(reason + ' 无 error 事件仍保留部分结果与终止说明', () => {
    const state = applyEvent(applyEvent(emptyTurn(), { type: 'final', content: '部分结论' }),
      { type: 'done', stopped_reason: reason, usage_complete: false })
    const warnings = runWarnings(state)
    assert.match(warnings[0].title, /部分结果/)
    assert.equal(warnings[0].tone, 'warn')
    assert.ok(warnings.some((warning) => /不完整/.test(warning.title)))
    assert.match(warnings.at(-1).text, /不按零/)
  })
}

test('取消不是成功结束，历史文本不冒充未知的本轮用量', () => {
  const state = { ...emptyTurn(), phase: 'aborted' }
  assert.match(runWarnings(state)[0].title, /停止/)
  assert.equal(runWarnings(state)[0].tone, 'warn')
  assert.deepEqual(runWarnings(state, true), [])
})

test('网络断流保留真实原因，不写成用户手动停止', () => {
  const warnings = runWarnings({ ...emptyTurn(), phase: 'aborted', error: '未收到 done，连接断开' })
  assert.match(warnings[0].title, /连接中断/)
  assert.match(warnings[0].text, /未收到 done/)
  assert.doesNotMatch(warnings[0].text, /已取消/)
})

test('API 错误未收到 done 仍按错误展示', () => {
  const warning = runWarnings({ ...emptyTurn(), phase: 'error', error: '访问被拒绝' })[0]
  assert.equal(warning.tone, 'error')
  assert.equal(warning.title, '执行出错')
  assert.equal(warning.text, '访问被拒绝')
})

test('正常完成仍单独说明裁剪；在途统计不提前标记不完整', () => {
  assert.deepEqual(runWarnings(emptyTurn()), [])
  const state = applyEvent(emptyTurn(), { type: 'done', stopped_reason: 'finished', usage_complete: true, context_trimmed: true })
  assert.deepEqual(runWarnings(state).map((warning) => warning.title), ['上下文已裁剪'])
})

test('计划进度按完成步骤统计，不将失败步骤算成功', () => {
  const state = { ...emptyTurn('thinking'), plan: { goal: '合成测试', reasoning: '', steps: [
    { id: 1, description: '已完成', status: 'done' },
    { id: 2, description: '失败', status: 'failed' },
    { id: 3, description: '读取公开样本', status: 'running' },
  ] } }
  assert.equal(executionActivity(state).text, '读取公开样本')
  assert.equal(executionActivity(state).progress, '1/3 步完成')
})

test('专家处理中和汇总阶段按真实事件切换，失败专家也算已结束', () => {
  const state = { ...emptyTurn('thinking'), delegations: [
    { name: '核验员', status: 'running' }, { name: '分析员', status: 'failed' },
  ] }
  assert.equal(executionActivity(state).text, '1 位专家处理中')
  assert.equal(executionActivity(state).progress, '1/2 位已结束')
  state.delegations[0].status = 'done'
  assert.equal(executionActivity(state).text, '正在汇总专家结论')
})
