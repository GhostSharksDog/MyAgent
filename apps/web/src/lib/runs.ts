import type { AgentMode, Usage } from './types'

export interface RunEventSummary {
  kind: string
  elapsed_ms: number
  step: number
  scope: 'main' | 'child'
  tool_name: string | null
  ok: boolean | null
  duration_ms: number | null
  truncated: boolean | null
  counts: Record<string, number> | null
}

export interface RunRecord {
  run_id: string
  session_id: string | null
  mode: AgentMode
  source: 'agent' | 'demo_replay'
  started_at: string
  finished_at: string | null
  duration_ms: number
  stopped_reason: string
  usage: Usage
  usage_complete: boolean
  steps_used: number
  tool_calls: number
  tool_results: number
  tool_failures: number
  context_trimmed: boolean
  context_tokens: number
  events_dropped: number
  events?: RunEventSummary[]
}

export interface RunList {
  backend: 'memory' | 'sql'
  total: number
  offset: number
  limit: number
  max_records: number
  summary_only: boolean
  runs: RunRecord[]
}

export function runsPath(offset = 0, sessionId: string | null = null, reason = ''): string {
  const query = new URLSearchParams({ limit: '50', offset: String(Math.max(0, offset)) })
  if (sessionId) query.set('session_id', sessionId)
  if (reason) query.set('stopped_reason', reason)
  return '/api/runs?' + query.toString()
}

export function describeRunEvent(event: RunEventSummary): string {
  const labels: Record<string, string> = {
    start: '开始执行', step: '模型推理', tool_call: '请求工具', tool_result: '工具返回',
    plan: '生成计划', plan_step: '更新计划进度', replan: '重新规划',
    delegate: '派发专家任务', delegate_result: '专家返回', done: '结束执行', error: '执行异常',
  }
  let text = labels[event.kind] ?? '执行事件'
  if (event.scope === 'child') text = '子任务 · ' + text
  if (event.tool_name) text += ' · ' + (event.tool_name === 'unknown_tool' ? '未注册工具' : event.tool_name)
  if (event.ok !== null) text += event.ok ? ' · 成功' : ' · 失败'
  if (event.duration_ms !== null) text += ` · ${event.duration_ms} ms`
  if (event.truncated) text += ' · 结果已截断'
  if (event.counts) text += ` · 完成 ${event.counts.done ?? 0} / 失败 ${event.counts.failed ?? 0} / 跳过 ${event.counts.skipped ?? 0}`
  return text
}
