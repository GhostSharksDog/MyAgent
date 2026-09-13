/**
 * 事件 → UI 状态归约器的自测。
 *
 * 这里喂的是**录制下来的一段真实事件序列**（一次带工具调用的多步对话），
 * 覆盖了流式 UI 最容易出错的几个点：
 *
 *   1. token 是增量，必须累加；累加完还要被 final 覆盖；
 *   2. 中间步骤的 token 是过渡语，不能混进最终答案；
 *   3. tool_result 必须按名字配对上对应的 tool_call（同一步可能有多个调用）；
 *   4. ERROR 事件≠故障：步数耗尽/死循环也会发 ERROR，
 *      真正的判据是 DONE 上的 stopped_reason；
 *   5. 未知事件类型必须被忽略而不是抛异常（后端将来会加事件类型）。
 *
 * 依赖的 `applyEvent` / `computeTurnView` 都是纯函数，不需要 React 或 DOM。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { applyEvent, computeTurnView, emptyTurn } from '../src/lib/stream.ts'

/** 把一串事件按顺序归约成最终状态。 */
function reduce(events) {
  return events.reduce((state, event) => applyEvent(state, event), emptyTurn())
}

const TOOL_TURN = [
  { type: 'start', content: '帮我看看简历和这个 JD 的匹配度' },
  { type: 'step', step: 1 },
  { type: 'token', step: 1, content: '我先' },
  { type: 'token', step: 1, content: '查一下你的简历。' },
  {
    type: 'tool_call',
    step: 1,
    tool_name: 'read_resume',
    tool_args: { path: 'resume.pdf' },
  },
  {
    type: 'tool_result',
    step: 1,
    tool_name: 'read_resume',
    tool_ok: true,
    content: '（简历正文…）',
    duration_ms: 12,
    truncated: true,
  },
  { type: 'step', step: 2 },
  { type: 'token', step: 2, content: '## 匹配度' },
  { type: 'token', step: 2, content: '：78 分' },
  { type: 'final', step: 2, content: '## 匹配度\n\n：78 分（这里是被 final 覆盖后的权威文本）' },
  {
    type: 'done',
    step: 2,
    steps_used: 2,
    usage: { prompt_tokens: 900, completion_tokens: 200, total_tokens: 1100 },
    stopped_reason: 'finished',
  },
]

test('完整多步序列：步骤、工具卡片、最终答案、用量都正确', () => {
  const turn = reduce(TOOL_TURN)

  assert.equal(turn.phase, 'done')
  assert.equal(turn.steps.length, 2)
  assert.equal(turn.steps[0].text, '我先查一下你的简历。')

  const call = turn.steps[0].toolCalls[0]
  assert.equal(call.name, 'read_resume')
  assert.deepEqual(call.args, { path: 'resume.pdf' })
  assert.equal(call.status, 'ok')
  assert.equal(call.durationMs, 12)
  assert.equal(call.truncated, true, 'truncated 必须被保留下来')

  assert.equal(turn.finalStep, 2)
  assert.match(turn.answer, /权威文本/)
  assert.equal(turn.usage.total_tokens, 1100)
  assert.equal(turn.stepsUsed, 2)
  assert.equal(turn.stoppedReason, 'finished')
  assert.equal(turn.error, null)
})

test('final 覆盖 token 累加结果（不是简单拼接）', () => {
  const turn = reduce(TOOL_TURN)
  assert.equal(
    turn.answer,
    '## 匹配度\n\n：78 分（这里是被 final 覆盖后的权威文本）',
    'final 必须是权威来源，而不是 token 的拼接',
  )
})

test('过渡语与最终答案被分开：时间线排除产出答案的那一步', () => {
  const view = computeTurnView(reduce(TOOL_TURN))
  assert.deepEqual(
    view.timeline.map((step) => step.index),
    [1],
    '第 1 步（过渡语 + 工具调用）应留在时间线，第 2 步（最终答案）不应重复出现',
  )
  assert.equal(view.answerIsFinal, true)
  assert.equal(view.toolCallCount, 1)
  assert.equal(view.runningTool, null)
  assert.equal(view.indicator, null)
})

test('流式中：当前步文本先作为答案预览，出现 tool_call 后被降级到时间线', () => {
  // 第 1 步刚吐出文本，还不知道它会不会调工具
  const streaming = reduce([
    { type: 'start' },
    { type: 'step', step: 1 },
    { type: 'token', step: 1, content: '我先查一下你的简历。' },
  ])
  const before = computeTurnView(streaming)
  assert.equal(before.answer, '我先查一下你的简历。', '未定稿时先进答案区做预览')
  assert.equal(before.answerIsFinal, false)
  assert.deepEqual(before.timeline, [], '预览中的那一步不该同时出现在时间线里')

  // 模型接着发起了工具调用 —— 那句话其实是过渡语
  const after = computeTurnView(
    applyEvent(streaming, { type: 'tool_call', step: 1, tool_name: 'read_resume', tool_args: {} }),
  )
  assert.equal(after.answer, '', '确认是过渡语后，答案区应当清空')
  assert.deepEqual(
    after.timeline.map((step) => step.index),
    [1],
    '该步应被降级到时间线',
  )
  assert.notEqual(after.runningTool, null, '此时应能报告"正在执行工具"')
})

test('等待首个 token 时给出 thinking 指示，连接阶段给出 connecting', () => {
  assert.equal(computeTurnView(emptyTurn('connecting')).indicator, 'connecting')

  const thinking = reduce([{ type: 'start' }, { type: 'step', step: 1 }])
  assert.equal(computeTurnView(thinking).indicator, 'thinking')

  const streaming = reduce([{ type: 'step', step: 1 }, { type: 'token', step: 1, content: 'a' }])
  assert.equal(computeTurnView(streaming).indicator, null, '已经在出字了就不该再显示"思考中"')
})

test('同一步多次工具调用：结果按名字配对，不会串位', () => {
  const turn = reduce([
    { type: 'step', step: 1 },
    { type: 'tool_call', step: 1, tool_name: 'search_jobs', tool_args: { q: 'a' } },
    { type: 'tool_call', step: 1, tool_name: 'read_resume', tool_args: {} },
    { type: 'tool_result', step: 1, tool_name: 'search_jobs', tool_ok: true, content: '岗位 A', duration_ms: 30 },
    { type: 'tool_result', step: 1, tool_name: 'read_resume', tool_ok: false, content: '文件不存在', duration_ms: 4 },
  ])

  const calls = turn.steps[0].toolCalls
  assert.equal(calls.length, 2)
  assert.equal(calls[0].status, 'ok')
  assert.equal(calls[0].output, '岗位 A')
  assert.equal(calls[1].status, 'failed')
  assert.equal(calls[1].output, '文件不存在')
})

test('没有配对的 tool_result 会被补成卡片，而不是静默丢弃', () => {
  const turn = reduce([
    { type: 'step', step: 1 },
    { type: 'tool_result', step: 1, tool_name: 'ghost_tool', tool_ok: true, content: 'x' },
  ])
  assert.equal(turn.steps[0].toolCalls.length, 1)
  assert.equal(turn.steps[0].toolCalls[0].name, 'ghost_tool')
})

test('ERROR + DONE(max_steps) 是预算终止，不是故障相', () => {
  const turn = reduce([
    { type: 'step', step: 1 },
    { type: 'error', content: '已达到单轮最大步数限制（6 步）仍未得到最终答案。' },
    { type: 'done', steps_used: 6, stopped_reason: 'max_steps', usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 } },
  ])

  assert.equal(turn.stoppedReason, 'max_steps')
  assert.match(turn.error, /最大步数/)
  assert.equal(turn.phase, 'done', '预算耗尽不应进入 error 相')
  assert.equal(turn.answer, '')
  assert.equal(computeTurnView(turn).isFailed, false)
})

test('DONE(error) 才是真正的故障相', () => {
  const turn = reduce([
    { type: 'error', content: '服务内部错误：连接超时' },
    { type: 'done', steps_used: 1, stopped_reason: 'error' },
  ])
  assert.equal(turn.phase, 'error')
  assert.equal(computeTurnView(turn).isFailed, true)
})

test('final 会清掉此前出现的 error（例如某一跳失败后模型自行修复）', () => {
  const turn = reduce([
    { type: 'error', step: 1, content: '一次工具失败' },
    { type: 'final', step: 2, content: '最终答案' },
    { type: 'done', steps_used: 2, stopped_reason: 'finished' },
  ])
  assert.equal(turn.error, null)
  assert.equal(turn.answer, '最终答案')
})

test('未知事件类型被忽略，不会破坏已有状态', () => {
  const before = reduce(TOOL_TURN)
  const after = applyEvent(before, { type: 'planning', content: '将来的新事件' })
  assert.deepEqual(after, before)
})

test('字段缺失的宽容性：只有 type 的事件也不该崩', () => {
  const turn = reduce([{ type: 'step' }, { type: 'token' }, { type: 'done' }])
  assert.equal(turn.steps.length, 1)
  assert.equal(turn.stoppedReason, 'finished', 'done 缺字段时回落到默认值')
  assert.equal(turn.usage, null)
})
