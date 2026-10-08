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

/** 确认与执行结果始终留在答案附近，不随执行过程或统计偏好折叠。 */
export const FileApprovalPanel = memo(function FileApprovalPanel({ approvals, active, disabled = false, assistantId, onDecide }: FileApprovalPanelProps) {
  if (!approvals.length) return null
  return <div className="file-approvals" aria-label="工具操作确认">
    {approvals.map((item) => {
      const command = item.kind === 'command'
      const external = item.kind === 'mcp'
      const memory = item.kind === 'memory'
      const status = describeApproval(item.status, item.kind ?? 'file', (command || external) && item.started === true)
      const actionable = active && !disabled && item.status === 'pending' && !!onDecide
      return <section key={item.id} className={`file-approval file-approval--${status.tone}`}
        aria-label={memory ? '保存长期记忆' : external ? `外部工具：${item.server_name} / ${item.tool_name}` : command ? `终端命令：${item.command}` : `文件修改：${item.path}`}
        data-kind={item.kind ?? 'file'} data-approval-id={item.id} data-status={item.status}>
        <header className="file-approval__head">
          <strong role="status">{status.title}</strong>
          <span className="file-approval__operation">{memory ? '长期记忆' : external ? 'MCP 外部工具' : command ? '本机终端' : OPERATIONS[item.operation]}</span>
        </header>
        {memory ? <><p className="file-approval__path">{item.fact}</p><p className="settings__hint">批准后保存到本机，在以后的任务中使用。你可以在记忆与存储设置中编辑或删除。</p></> : external ? <>
          <p className="file-approval__path">{item.server_name} / <code>{item.tool_name}</code></p>
          <div className="command-approval__command" role="region" aria-label="完整外部调用参数" tabIndex={0}>
            <pre>{JSON.stringify(item.arguments, null, 2)}</pre>
          </div>
          <p className="file-approval__warning">批准后把上述参数发送到此服务。访问范围由外部服务配置决定，内置文件和终端权限不限制它；已发生的操作不会自动撤销。</p>
        </> : command ? <>
          <dl className="command-approval__details">
            <div><dt>起始目录</dt><dd><code>{item.cwd}</code></dd></div>
            <div><dt>Shell</dt><dd><code>{item.shell}</code></dd></div>
            <div><dt>执行时限</dt><dd>{item.timeout_seconds} 秒</dd></div>
          </dl>
          <div className="command-approval__command" role="region" aria-label="完整终端命令" tabIndex={0}>
            <pre>{item.command}</pre>
          </div>
          <p className="file-approval__warning"><strong>批准后以服务账户权限执行。</strong>
            命令可访问任意文件、联网或启动程序；起始目录不构成沙箱，文件写权限与敏感文件开关不限制它。
            已产生的副作用不会自动撤销。</p>
        </> : <>
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
        </>}
        <p className="file-approval__message">{item.message || (command
          ? '请核对完整命令与起始目录。批准后再次检查权限，每条命令都需要独立确认。'
          : external ? '请核对服务、工具和完整参数。' : '请核对完整差异。批准后服务将重新核验文件，确认未变化后才写入。')}</p>
        {item.error && <p className="file-approval__error" role="alert">{item.error}</p>}
        {item.status === 'pending' && <div className="file-approval__actions">
          <button type="button" className="btn btn--primary" disabled={!actionable || item.busy}
            onClick={() => void onDecide?.(assistantId, item.id, 'approve')}>{memory ? '确认保存记忆' : external ? '批准外部调用' : command ? '批准执行' : '批准此修改'}</button>
          <button type="button" className="btn" disabled={!actionable || item.busy}
            onClick={() => void onDecide?.(assistantId, item.id, 'reject')}>{memory ? '不保存' : external ? '拒绝外部调用' : command ? '拒绝执行' : '拒绝修改'}</button>
          {item.busy && <span role="status">正在提交决定…</span>}
          {!active && <span>本轮已关闭，不能再确认</span>}
          {active && disabled && <span>正在切换对话，暂时不能确认</span>}
        </div>}
      </section>
    })}
  </div>
})
