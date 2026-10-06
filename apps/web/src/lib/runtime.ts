/** 预算终态和设置校验供组件与离线测试共用。 */
export function describeStop(reason: string | null): { text: string; tone: 'ok' | 'warn' | 'danger' } {
  switch (reason) {
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
