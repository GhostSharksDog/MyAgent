import { isTurnRunning, runWarnings } from '../lib/runtime'
import { computeTurnView } from '../lib/stream'
import { formatCompact, formatNumber } from '../lib/format'
import type { ChatItem, FileApprovalDecision } from '../lib/types'
import { ExecutionProcess } from './ExecutionProcess'
import { IconCoins, IconLayers } from './Icons'
import { Markdown } from './Markdown'
import { Notice } from './Notice'
import { FileApprovalPanel } from './FileApprovalPanel'

type AssistantItem = Extract<ChatItem, { kind: 'assistant' }>

export interface AssistantTurnProps { item: AssistantItem; showMeta?: boolean; onOpenRun?: (id: string) => void;
  active?: boolean; approvalDisabled?: boolean;
  onDecideApproval?: (assistantId: string, id: string, decision: FileApprovalDecision) => Promise<void> }

export function AssistantTurn({ item, showMeta = true, onOpenRun, active = false, approvalDisabled, onDecideApproval }: AssistantTurnProps) {
  const { state } = item
  const view = computeTurnView(state)
  const streaming = isTurnRunning(state)
  const warnings = runWarnings(state, item.restored)
  return (
    <article className="turn">
      <header className="turn__head">
        <span className="turn__avatar" aria-hidden="true">L</span><span>Legacy</span>
        {streaming && <span className="dot dot--live" />}
        {item.restored && <span className="turn__restored" title="会话仅保存问答文本">历史回答 · 不含执行过程</span>}
      </header>
      <ExecutionProcess state={state} timeline={view.timeline} />
      <div className={'answer' + (view.answerIsFinal ? '' : ' answer--partial')}>
        {view.answer ? <div className="answer__body"><Markdown source={view.answer} />
          {!view.answerIsFinal && streaming && <span className="caret" />}</div>
          : streaming ? <div className="thinking" role="status">
            <span className="thinking__dots"><span /><span /><span /></span>
            <span>{state.phase === 'connecting' ? '正在连接…' : state.approvals.some((approval) => approval.status === 'pending')
              ? '请核对下方操作预览并决定是否批准' : view.runningTool ? '正在调用 ' + view.runningTool.name : '正在思考…'}</span>
          </div> : !state.error && !item.restored ? <p className="muted">本轮没有产出答案，可展开执行过程查看已有内容。</p> : null}
      </div>
      <FileApprovalPanel approvals={state.approvals} active={active && streaming && !!state.runId}
        disabled={approvalDisabled} assistantId={item.id} onDecide={onDecideApproval} />
      {!!warnings.length && <div className="run-warnings" data-testid="run-warnings">
        {warnings.map((warning) => <Notice key={warning.title} tone={warning.tone}
          role={warning.tone === 'error' ? 'alert' : undefined} title={warning.title} text={warning.text} />)}
      </div>}
      {showMeta && !item.restored && <div className="runmeta">
        <span className="runmeta__item"><IconLayers size={12} />{state.stepsUsed || state.steps.length} 步</span>
        {!!view.toolCallCount && <span className="runmeta__item">{view.toolCallCount} 次工具调用</span>}
        {state.usage && <span className="runmeta__item" title="输入 / 输出 / 合计 token"><IconCoins size={12} />
          {formatCompact(state.usage.prompt_tokens)} / {formatCompact(state.usage.completion_tokens)} / {formatNumber(state.usage.total_tokens)} tokens
        </span>}
      </div>}
      {state.runId && onOpenRun && <button type="button" className="link-button turn__record"
        onClick={() => onOpenRun(state.runId!)}>查看运行摘要</button>}
    </article>
  )
}
