import type { AssistantTurnState } from './types'

/** 预算终态和设置校验供组件与离线测试共用。 */
export function describeStop(reason: string | null): { text: string; tone: 'ok' | 'warn' | 'danger' } {
  switch (reason) {
    case 'running': return { text: '执行中', tone: 'ok' }
    case 'cancelled': return { text: '已取消', tone: 'warn' }
    case 'interrupted': return { text: '服务中断', tone: 'warn' }
    case 'finished': return { text: '正常结束', tone: 'ok' }
    case 'timeout': return { text: '达到整轮时长预算', tone: 'warn' }
    case 'token_budget': return { text: '达到累计 token 预算', tone: 'warn' }
    case 'max_steps': return { text: '达到步数上限', tone: 'warn' }
    case 'loop_detected': return { text: '检测到重复调用，已中止', tone: 'warn' }
    case 'error': return { text: '执行出错', tone: 'danger' }
    case null: return { text: '未收到结束事件', tone: 'warn' }
    default: return { text: reason, tone: 'warn' }
  }
}

export function parseTokenBudget(value: string): number | null {
  if (!/^\d+$/.test(value.trim())) return null
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null
}

export function isTurnRunning(state: AssistantTurnState): boolean {
  return state.phase === 'connecting' || state.phase === 'thinking' || state.phase === 'streaming'
}

export interface RunWarning { title: string; text: string; tone: 'warn' | 'error' | 'info' }

/** 这些状态属于答案的可靠性说明，不受执行详情和统计显示偏好控制。 */
export function runWarnings(state: AssistantTurnState, restored = false): RunWarning[] {
  if (restored) return [] // 历史仅保存文本，不能把未保存的统计当作本次模型缺失用量。
  const warnings: RunWarning[] = []
  if (state.sessionSaved === false) warnings.push({ title: '会话未保存',
    text: '本轮记录未能保存，下轮可能无法引用此次对话或执行事实。请检查会话存储。', tone: 'warn' })
  if (state.recordSaved === false) warnings.push({ title: '运行摘要未保存',
    text: '请检查服务日志和运行记录的存储配置。当前回答仍可查看。', tone: 'warn' })
  const running = isTurnRunning(state)
  if (!running && state.phase === 'aborted') {
    warnings.push({ title: state.error ? '连接中断 · 结果可能不完整' : '已停止生成',
      text: state.error ?? '保留已收到的内容，后续请求已取消。', tone: 'warn' })
  } else if ((!running && state.stoppedReason !== 'finished') || state.error) {
    const stop = describeStop(state.phase === 'error' ? 'error' : state.stoppedReason)
    warnings.push({ title: (state.answer ? '部分结果 · ' : '') + stop.text,
      text: state.error ?? '本轮已终止，已有内容可供参考。', tone: stop.tone === 'danger' ? 'error' : 'warn' })
  }
  if (state.contextTrimmed) warnings.push({ title: '上下文已裁剪',
    text: '本轮已移除最早的对话内容，回答可能无法参考这些历史。', tone: 'info' })
  if (!running && !state.usageComplete) warnings.push({ title: '用量统计不完整',
    text: state.usage ? '仅显示已返回的用量，未知消耗未计入。' : '模型未返回完整用量，未知消耗不按零计算。', tone: 'info' })
  return warnings
}

/** 汇总真实进度；不根据输出长度编造百分比。 */
export function executionActivity(state: AssistantTurnState): { text: string; progress: string } {
  const tools = state.steps.flatMap((step) => step.toolCalls)
  const runningTool = tools.find((call) => call.status === 'running')
  const running = isTurnRunning(state)
  const pending = state.approvals.filter((item) => item.status === 'pending')
  if (running && pending.length) return {
    text: pending.every((item) => item.kind === 'command') ? '等待批准终端命令'
      : pending.every((item) => item.kind === 'mcp') ? '等待批准外部工具'
        : pending.every((item) => !item.kind || item.kind === 'file') ? '等待批准文件修改' : '等待批准工具操作',
    progress: pending.length + ' 项待确认',
  }
  if (state.plan) {
    const steps = state.plan.steps
    const active = steps.find((step) => step.status === 'running')
    return { text: running ? (runningTool ? '正在调用 ' + runningTool.name : active?.description ?? '正在整理计划结果') : '计划执行结束',
      progress: steps.filter((step) => step.status === 'done').length + '/' + steps.length + ' 步完成' }
  }
  if (state.delegations.length) {
    const active = state.delegations.filter((item) => item.status === 'running')
    return { text: running ? (runningTool ? '正在调用 ' + runningTool.name : active.length ? active.length + ' 位专家处理中' : '正在汇总专家结论') : '专家协作结束',
      progress: state.delegations.filter((item) => item.status !== 'running').length + '/' + state.delegations.length + ' 位已结束' }
  }
  return { text: running ? (runningTool ? '正在调用 ' + runningTool.name : '正在分析问题') : '执行结束',
    progress: (state.stepsUsed || state.steps.length) + ' 步' + (tools.length ? ' · ' + tools.length + ' 次工具调用' : '') }
}
