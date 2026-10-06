import type { FileApproval, FileApprovalStatus, FileApprovalView } from './types'

const STATUSES: FileApprovalStatus[] = ['pending', 'approved', 'rejected', 'expired', 'cancelled', 'conflict', 'applied', 'failed']

/** 预览来自网络，校验完整结构后才允许显示操作。 */
export function readApproval(value: unknown): FileApproval | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  if (typeof item.id !== 'string' || !item.id || typeof item.path !== 'string' || !item.path ||
    !['create', 'overwrite', 'edit'].includes(String(item.operation)) ||
    typeof item.diff !== 'string' || typeof item.message !== 'string' ||
    !STATUSES.includes(item.status as FileApprovalStatus) ||
    !Number.isSafeInteger(item.before_bytes) || (item.before_bytes as number) < 0 ||
    !Number.isSafeInteger(item.after_bytes) || (item.after_bytes as number) < 0 ||
    (item.before_format !== undefined && typeof item.before_format !== 'string') ||
    (item.after_format !== undefined && typeof item.after_format !== 'string')) return null
  return item as unknown as FileApproval
}

export function mergeApproval(items: FileApprovalView[], proposal: FileApproval): FileApprovalView[] {
  const previous = items.find((item) => item.id === proposal.id)
  // 已决状态不能因迟到的 request 或 HTTP 响应退回等待；applied 才是成功。
  if (previous && previous.status !== 'pending' && previous.status !== 'approved') return items
  if (previous?.status === 'approved' && proposal.status === 'pending') return items
  const next = { ...proposal, busy: proposal.status === 'pending' && previous?.busy === true,
    error: proposal.status === 'pending' ? previous?.error ?? null : null }
  return previous ? items.map((item) => item.id === proposal.id ? next : item) : [...items, next]
}

export function closeApprovals(items: FileApprovalView[], message: string): FileApprovalView[] {
  return items.map((item) => item.status === 'pending' || item.status === 'approved'
    ? { ...item, status: 'cancelled', busy: false, error: null, message } : item)
}

export function describeApproval(status: FileApprovalStatus): { title: string; tone: 'pending' | 'ok' | 'warn' | 'error' } {
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
