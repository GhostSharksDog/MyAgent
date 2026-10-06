import { memo } from 'react'

import { describeApproval, diffLineKind } from '../lib/approvals'
import { formatNumber } from '../lib/format'
import type { FileApprovalDecision, FileApprovalView } from '../lib/types'

interface FileApprovalPanelProps {
  approvals: FileApprovalView[]
  active: boolean
  disabled?: boolean
  assistantId: string
  onDecide?: (assistantId: string, id: string, decision: FileApprovalDecision) => Promise<void>
}

const OPERATIONS = { create: '新建文件', overwrite: '覆盖文件', edit: '编辑文件' }

/** 确认与写入结果始终留在答案附近，不随执行过程或统计偏好折叠。 */
export const FileApprovalPanel = memo(function FileApprovalPanel({ approvals, active, disabled = false, assistantId, onDecide }: FileApprovalPanelProps) {
  if (!approvals.length) return null
  return <div className="file-approvals" aria-label="文件修改确认">
    {approvals.map((item) => {
      const status = describeApproval(item.status)
      const actionable = active && !disabled && item.status === 'pending' && !!onDecide
      return <section key={item.id} className={`file-approval file-approval--${status.tone}`}
        aria-label={`文件修改：${item.path}`} data-approval-id={item.id} data-status={item.status}>
        <header className="file-approval__head">
          <strong role="status">{status.title}</strong>
          <span className="file-approval__operation">{OPERATIONS[item.operation]}</span>
        </header>
        <p className="file-approval__path"><code>{item.path}</code></p>
        <p className="file-approval__size">{formatNumber(item.before_bytes)} → {formatNumber(item.after_bytes)} 字节</p>
        {(item.before_format || item.after_format) && <div className="file-approval__formats">
          {item.before_format && <span>修改前：{item.before_format}</span>}
          {item.after_format && <span>修改后：{item.after_format}</span>}
        </div>}
        <div className="file-approval__diff" role="region" aria-label={`${item.path} 完整修改差异`} tabIndex={0}>
          {item.diff ? <pre>{item.diff.split('\n').map((line, index) =>
            <span key={index} className={`diff-line diff-line--${diffLineKind(line)}`}>{line}{'\n'}</span>)}</pre>
            : <p>文本内容没有差异，请核对上方编码、换行和字节信息。</p>}
        </div>
        <p className="file-approval__message">{item.message || '请核对完整差异。批准后服务将重新核验文件，确认未变化后才写入。'}</p>
        {item.error && <p className="file-approval__error" role="alert">{item.error}</p>}
        {item.status === 'pending' && <div className="file-approval__actions">
          <button type="button" className="btn btn--primary" disabled={!actionable || item.busy}
            onClick={() => void onDecide?.(assistantId, item.id, 'approve')}>批准此修改</button>
          <button type="button" className="btn" disabled={!actionable || item.busy}
            onClick={() => void onDecide?.(assistantId, item.id, 'reject')}>拒绝修改</button>
          {item.busy && <span role="status">正在提交决定…</span>}
          {!active && <span>本轮已关闭，不能再确认</span>}
          {active && disabled && <span>正在切换对话，暂时不能确认</span>}
        </div>}
      </section>
    })}
  </div>
})
