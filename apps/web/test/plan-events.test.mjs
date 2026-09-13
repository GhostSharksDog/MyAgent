/**
 * 计划事件与专家派发事件的归约测试。
 *
 * 这两组事件属于 P3 新增的两种 Agent 形态（Plan-and-Execute / 多 Agent），
 * 它们的归约逻辑有两个**非平凡**的决策，正是这里要守住的：
 *
 *   1. **计划是快照语义，不是 diff**。后端每个计划事件都带完整计划，
 *      前端直接整体替换。如果误写成"合并字段"，就会出现"某一步的状态
 *      永远停在旧值"这种极难察觉的 bug（因为大部分字段是对的）。
 *      测试里用"新快照删掉了一个步骤"来验证——合并实现会留下幽灵步骤。
 *
 *   2. **同名专家可能被派发多次**。配对必须"从后往前找最近一个仍在运行的
 *      同名派发"，否则第二次的结果会写到第一张卡片上，
 *      表现为"第一张卡片显示了第二张的结论"。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { applyEvent, emptyTurn } from '../src/lib/stream.ts'

function reduce(events) {
  return events.reduce((state, event) => applyEvent(state, event), emptyTurn())
}

function plan(goal, steps) {
  return { goal, steps, reasoning: '因为任务可以拆开' }
}

function step(id, status, extra = {}) {
  return { id, description: `步骤${id}`, status, ...extra }
}

// ============================================================
// 计划
// ============================================================

test('plan 事件写入完整快照', () => {
  const state = reduce([
    { type: 'start' },
    {
      type: 'plan',
      plan: plan('分析简历', [step(1, 'pending'), step(2, 'pending')]),
    },
  ])

  assert.ok(state.plan)
  assert.equal(state.plan.goal, '分析简历')
  assert.equal(state.plan.steps.length, 2)
  assert.equal(state.plan.reasoning, '因为任务可以拆开')
})

test('plan_step 是整体替换而不是字段合并', () => {
  // 关键：第二个快照**只剩一个步骤**。
  // 合并实现会把第一步留下来变成幽灵步骤。
  const state = reduce([
    { type: 'plan', plan: plan('目标', [step(1, 'pending'), step(2, 'pending')]) },
    { type: 'plan_step', plan: plan('目标', [step(2, 'running')]) },
  ])

  assert.equal(state.plan.steps.length, 1, '快照被当成 diff 合并了：幽灵步骤没被清掉')
  assert.equal(state.plan.steps[0].status, 'running')
})

test('replan 同样整体替换', () => {
  const state = reduce([
    { type: 'plan', plan: plan('目标', [step(1, 'pending')]) },
    {
      type: 'replan',
      plan: plan('目标', [
        step(1, 'done', { result: '结论1' }),
        step(2, 'failed', { error: '缺数据' }),
        step(3, 'pending'),
      ]),
    },
  ])

  assert.equal(state.plan.steps.length, 3)
  assert.equal(state.plan.steps[0].result, '结论1')
  assert.equal(state.plan.steps[1].error, '缺数据')
})

test('步骤结论与失败原因都能读出来', () => {
  const state = reduce([
    {
      type: 'plan',
      plan: plan('目标', [
        step(1, 'done', { result: '用户有 3 年后端经验' }),
        step(2, 'failed', { error: '知识库中没有相关内容' }),
        step(3, 'skipped', { error: '预算耗尽' }),
      ]),
    },
  ])

  assert.equal(state.plan.steps[0].result, '用户有 3 年后端经验')
  assert.equal(state.plan.steps[1].error, '知识库中没有相关内容')
  assert.equal(state.plan.steps[2].status, 'skipped')
})

test('结构非法的计划被忽略，不污染状态', () => {
  // 事件类型放宽成 string 是为了向后兼容，代价是 plan 字段在类型上不受信。
  // 非法结构必须退回"没有计划"，而不是让归约崩掉或写入半个对象。
  for (const bad of [
    { type: 'plan', plan: null },
    { type: 'plan', plan: {} },
    { type: 'plan', plan: { goal: 123, steps: [] } },
    { type: 'plan', plan: { goal: 'g', steps: 'not-an-array' } },
  ]) {
    const state = reduce([bad])
    assert.equal(state.plan, null, `非法计划被写入了状态：${JSON.stringify(bad)}`)
  }
})

test('计划事件不覆盖已有的答案文本', () => {
  const state = reduce([
    { type: 'token', step: 1, content: '一些文本' },
    { type: 'plan', plan: plan('目标', [step(1, 'pending')]) },
  ])
  assert.equal(state.steps[0].text, '一些文本')
  assert.ok(state.plan)
})

// ============================================================
// 专家派发
// ============================================================

test('delegate 新增一张运行中的卡片', () => {
  const state = reduce([
    { type: 'delegate', specialist: '简历诊断师', content: '当需要评价简历时找它' },
  ])

  assert.equal(state.delegations.length, 1)
  assert.equal(state.delegations[0].name, '简历诊断师')
  assert.equal(state.delegations[0].status, 'running')
  assert.equal(state.delegations[0].brief, '当需要评价简历时找它')
})

test('delegate_result 配对上对应的派发', () => {
  const state = reduce([
    { type: 'delegate', specialist: '简历诊断师' },
    { type: 'delegate_result', specialist: '简历诊断师', tool_ok: true, content: '诊断结论' },
  ])

  assert.equal(state.delegations[0].status, 'ok')
  assert.equal(state.delegations[0].output, '诊断结论')
})

test('失败的专家标记为 failed 并保留错误', () => {
  const state = reduce([
    { type: 'delegate', specialist: '岗位分析师' },
    {
      type: 'delegate_result',
      specialist: '岗位分析师',
      tool_ok: false,
      content: 'RuntimeError: 岗位库不可用',
    },
  ])

  assert.equal(state.delegations[0].status, 'failed')
  assert.match(state.delegations[0].output, /岗位库不可用/)
})

test('同名专家被派发两次时，结果分别落到各自的卡片', () => {
  // 这是"从后往前找最近一个 running 的同名派发"这条规则存在的理由。
  // 如果按名字取第一个匹配，第二次的结果会覆盖第一张卡片。
  const state = reduce([
    { type: 'delegate', specialist: '匹配度顾问' },
    { type: 'delegate', specialist: '匹配度顾问' },
    {
      type: 'delegate_result',
      specialist: '匹配度顾问',
      tool_ok: true,
      content: '第一次的结论',
    },
  ])

  assert.equal(state.delegations.length, 2)
  assert.equal(state.delegations[0].status, 'ok')
  assert.equal(state.delegations[0].output, '第一次的结论')
  assert.equal(state.delegations[1].status, 'running', '第二次派发被错误地配对了')

  const after = applyEvent(state, {
    type: 'delegate_result',
    specialist: '匹配度顾问',
    tool_ok: true,
    content: '第二次的结论',
  })
  assert.equal(after.delegations[0].output, '第一次的结论')
  assert.equal(after.delegations[1].output, '第二次的结论')
})

test('多专家并发返回时各归各的', () => {
  // 后端用 as_completed 先完成先出结果，顺序与派发顺序无关。
  const state = reduce([
    { type: 'delegate', specialist: 'A' },
    { type: 'delegate', specialist: 'B' },
    { type: 'delegate', specialist: 'C' },
    { type: 'delegate_result', specialist: 'C', tool_ok: true, content: 'C 的结论' },
    { type: 'delegate_result', specialist: 'A', tool_ok: true, content: 'A 的结论' },
  ])

  const byName = Object.fromEntries(state.delegations.map((d) => [d.name, d]))
  assert.equal(byName.C.output, 'C 的结论')
  assert.equal(byName.A.output, 'A 的结论')
  assert.equal(byName.B.status, 'running')
})

test('没有配对派发的 delegate_result 会补一张卡片', () => {
  // 事件流被裁剪过时可能发生。可见的异常远好过"少了一张卡片却没人知道"。
  const state = reduce([
    { type: 'delegate_result', specialist: '幽灵专家', tool_ok: true, content: '结论' },
  ])

  assert.equal(state.delegations.length, 1)
  assert.equal(state.delegations[0].name, '幽灵专家')
  assert.equal(state.delegations[0].status, 'ok')
})

test('缺少 specialist 字段时不崩', () => {
  const state = reduce([{ type: 'delegate' }, { type: 'delegate_result' }])
  assert.equal(state.delegations.length, 1)
  assert.equal(state.delegations[0].name, '(未命名专家)')
})

test('多 Agent 轮次不会被认为是规划型轮次', () => {
  const state = reduce([
    { type: 'delegate', specialist: 'A' },
    { type: 'delegate_result', specialist: 'A', tool_ok: true, content: 'x' },
    { type: 'final', content: '最终答案' },
    { type: 'done', stopped_reason: 'finished' },
  ])

  assert.equal(state.plan, null)
  assert.equal(state.delegations.length, 1)
  assert.equal(state.answer, '最终答案')
  assert.equal(state.phase, 'done')
})

test('未知事件类型仍然被忽略（向后兼容）', () => {
  const state = reduce([
    { type: 'plan', plan: plan('目标', [step(1, 'pending')]) },
    { type: 'some_future_event', content: '未来才会有的事件' },
  ])
  assert.equal(state.plan.steps.length, 1)
})
