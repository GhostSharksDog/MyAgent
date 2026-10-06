import assert from 'node:assert/strict'
import test from 'node:test'
import { describeRunEvent, runsPath } from '../src/lib/runs.ts'
import { applyEvent, emptyTurn } from '../src/lib/stream.ts'
import { describeStop, runWarnings } from '../src/lib/runtime.ts'

test('运行筛选编码会话，不把筛选值拼进另一个参数', () => {
  const path = new URL(runsPath(50, 'session&stopped_reason=finished', 'cancelled'), 'http://local')
  assert.equal(path.searchParams.get('session_id'), 'session&stopped_reason=finished')
  assert.equal(path.searchParams.get('stopped_reason'), 'cancelled')
  assert.equal(path.searchParams.get('offset'), '50')
  assert.equal(new URL(runsPath(-1), 'http://local').searchParams.get('offset'), '0')
})

test('子任务失败、耗时与截断都能在摘要中看到', () => {
  assert.equal(describeRunEvent({kind:'tool_result',scope:'child',tool_name:'read_file',ok:false,duration_ms:12,truncated:true,counts:null}),
    '子任务 · 工具返回 · read_file · 失败 · 12 ms · 结果已截断')
})

test('未注册工具与未知事件不复述任意模型名称', () => {
  assert.equal(describeRunEvent({kind:'sensitive-content',scope:'main',tool_name:'unknown_tool',ok:null,duration_ms:null,truncated:null,counts:null}),
    '执行事件 · 未注册工具')
})

test('运行标识贯穿事件，结束后仍可打开摘要', () => {
  const start = applyEvent(emptyTurn(), {type:'start',run_id:'run-a'})
  const done = applyEvent(start, {type:'done',stopped_reason:'token_budget',usage_complete:false,record_saved:true})
  assert.equal(done.runId, 'run-a')
  assert.equal(done.recordSaved, true)
  assert.equal(done.stoppedReason, 'token_budget')
  assert.equal(start.recordSaved, undefined)
})

test('记录保存失败直接提醒，不能藏进运行统计', () => {
  const done = applyEvent(emptyTurn(), {type:'done',stopped_reason:'finished',record_saved:false,usage_complete:true})
  assert.equal(runWarnings(done)[0].title, '运行摘要未保存')
})

test('运行中、取消和重启中断具有独立状态', () => {
  assert.equal(describeStop('running').text, '执行中')
  assert.equal(describeStop('cancelled').text, '已取消')
  assert.equal(describeStop('interrupted').text, '服务中断')
})
