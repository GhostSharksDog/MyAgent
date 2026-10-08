import type { Approval, FileApprovalStatus, FileApprovalView } from './types'

const STATUSES: FileApprovalStatus[] = ['pending', 'approved', 'rejected', 'expired', 'cancelled', 'conflict', 'applied', 'failed']

/** 预览来自网络，校验完整结构后才允许显示操作。 */
export function readApproval(value: unknown): Approval | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  if (typeof item.id !== 'string' || !item.id.trim() || typeof item.message !== 'string' ||
    !STATUSES.includes(item.status as FileApprovalStatus)) return null
  if (item.kind === 'mcp') {
    if (typeof item.server_id !== 'string' || !item.server_id ||
      typeof item.server_name !== 'string' || !item.server_name ||
      typeof item.tool_name !== 'string' || !item.tool_name ||
      typeof item.fingerprint !== 'string' || !/^[a-f0-9]{64}$/.test(item.fingerprint) ||
      !item.arguments || typeof item.arguments !== 'object' || Array.isArray(item.arguments) ||
      typeof item.started !== 'boolean' || (item.started && item.status === 'pending')) return null
    return item as unknown as Approval
  }
  if (item.kind === 'command') {
    if (typeof item.command !== 'string' || !item.command.trim() || item.command.length > 16000 ||
      item.command.includes('\0') || typeof item.cwd !== 'string' || !item.cwd.trim() ||
      item.cwd.includes('\0') || !/^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(item.cwd) ||
      typeof item.shell !== 'string' || !item.shell.trim() ||
      (item.started !== undefined && typeof item.started !== 'boolean') ||
      (item.started === true && item.status === 'pending') ||
      typeof item.timeout_seconds !== 'number' || !Number.isFinite(item.timeout_seconds) ||
      item.timeout_seconds <= 0 || item.timeout_seconds > 600) return null
    return item as unknown as Approval
  }
  if (item.kind !== undefined && item.kind !== 'file') return null
  if (typeof item.path !== 'string' || !item.path ||
    !['create', 'overwrite', 'edit'].includes(String(item.operation)) ||
    typeof item.diff !== 'string' || typeof item.message !== 'string' ||
    !STATUSES.includes(item.status as FileApprovalStatus) ||
    !Number.isSafeInteger(item.before_bytes) || (item.before_bytes as number) < 0 ||
    !Number.isSafeInteger(item.after_bytes) || (item.after_bytes as number) < 0 ||
    (item.before_format !== undefined && typeof item.before_format !== 'string') ||
    (item.after_format !== undefined && typeof item.after_format !== 'string')) return null
  return item as unknown as Approval
}

export function mergeApproval(items: FileApprovalView[], proposal: Approval): FileApprovalView[] {
  const previous = items.find((item) => item.id === proposal.id)
  // 同一 ID 不能从文件切成命令，也不能在等待中替换将要批准的操作。
  if (previous && !sameOperation(previous, proposal)) return items
  // 已决状态不能因迟到的 request 或 HTTP 响应退回等待；applied 才是成功。
  if (previous && previous.status !== 'pending' && previous.status !== 'approved') return items
  if (previous?.status === 'approved' && proposal.status === 'pending') return items
  const next = { ...proposal, busy: proposal.status === 'pending' && previous?.busy === true,
    error: proposal.status === 'pending' ? previous?.error ?? null : null }
  if (next.kind === 'command' && previous?.kind === 'command' && previous.started === true) next.started = true
  if (next.kind === 'mcp' && previous?.kind === 'mcp' && previous.started) next.started = true
  return previous ? items.map((item) => item.id === proposal.id ? next : item) : [...items, next]
}

function sameOperation(before: Approval, after: Approval): boolean {
  if (before.kind === 'mcp') return after.kind === 'mcp' &&
    before.server_id === after.server_id && before.server_name === after.server_name &&
    before.tool_name === after.tool_name && before.fingerprint === after.fingerprint &&
    canonical(before.arguments) === canonical(after.arguments)
  if (before.kind === 'command') return after.kind === 'command' &&
    before.command === after.command && before.cwd === after.cwd && before.shell === after.shell &&
    before.timeout_seconds === after.timeout_seconds
  return after.kind !== 'command' && after.kind !== 'mcp' && before.path === after.path && before.operation === after.operation &&
    before.diff === after.diff && before.before_bytes === after.before_bytes && before.after_bytes === after.after_bytes &&
    before.before_format === after.before_format && before.after_format === after.after_format
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  if (value && typeof value === 'object') return '{' + Object.entries(value).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => JSON.stringify(key) + ':' + canonical(item)).join(',') + '}'
  return JSON.stringify(value) ?? 'null'
}

export function closeApprovals(items: FileApprovalView[], message: string): FileApprovalView[] {
  return items.map((item) => item.status === 'pending' || item.status === 'approved'
    ? { ...item, status: 'cancelled', busy: false, error: null,
      message: item.kind === 'mcp' ? '本轮已关闭，外部调用结果未确认；已发送的操作可能仍在服务端执行，不会自动重发或撤销。' : item.kind === 'command'
        ? '本轮已关闭，不能再批准命令；未收到执行完成事件。已产生的副作用不会自动撤销。' : message } : item)
}

export function describeApproval(status: FileApprovalStatus, kind: 'file' | 'command' | 'mcp' = 'file', started = false): { title: string; tone: 'pending' | 'ok' | 'warn' | 'error' } {
  if (kind === 'mcp') {
    switch (status) {
      case 'pending': return { title: '等待批准外部工具', tone: 'pending' }
      case 'approved': return { title: started ? '外部工具执行中' : '已批准 · 等待调用', tone: 'pending' }
      case 'applied': return { title: '外部调用完成', tone: 'ok' }
      case 'rejected': return { title: '已拒绝 · 未调用', tone: 'warn' }
      case 'expired': return { title: '确认已超时 · 未调用', tone: 'warn' }
      case 'cancelled': return { title: started ? '外部调用结果未确认' : '确认已关闭', tone: 'warn' }
      case 'conflict': return { title: '服务或工具已变化 · 需要重新确认', tone: 'warn' }
      case 'failed': return { title: '外部调用未成功', tone: 'error' }
    }
  }
  if (kind === 'command') {
    switch (status) {
      case 'pending': return { title: '等待批准终端命令', tone: 'pending' }
      case 'approved': return { title: started ? '已批准 · 正在执行' : '已批准 · 等待执行', tone: 'pending' }
      case 'applied': return { title: '命令执行完成', tone: 'ok' }
      case 'rejected': return { title: '已拒绝 · 未执行此命令', tone: 'warn' }
      case 'expired': return { title: '确认已超时 · 未执行此命令', tone: 'warn' }
      case 'cancelled': return { title: '命令已取消', tone: 'warn' }
      case 'conflict': return { title: '执行条件已变化 · 需要重新确认', tone: 'warn' }
      case 'failed': return { title: '命令执行失败', tone: 'error' }
    }
  }
  switch (status) {
    case 'pending': return { title: '等待批准文件修改', tone: 'pending' }
    case 'approved': return { title: '已批准 · 正在核验', tone: 'pending' }
    case 'applied': return { title: '文件已写入', tone: 'ok' }
    case 'rejected': return { title: '已拒绝 · 未应用此修改', tone: 'warn' }
    case 'expired': return { title: '确认已超时 · 未应用此修改', tone: 'warn' }
    case 'cancelled': return { title: '确认已关闭', tone: 'warn' }
    case 'conflict': return { title: '修改条件已变化 · 需要重新预览', tone: 'warn' }
    case 'failed': return { title: '文件修改失败', tone: 'error' }
  }
}

export function parseApprovalTimeout(value: string): number | null {
  if (!value.trim()) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null
}

export function diffLineKind(line: string): 'header' | 'add' | 'remove' | 'context' {
  if (line.startsWith('+++ ') || line.startsWith('--- ') || line.startsWith('@@')) return 'header'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'remove'
  return 'context'
}
