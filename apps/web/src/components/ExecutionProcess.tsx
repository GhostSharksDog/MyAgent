import { useState } from 'react'
import { executionActivity, isTurnRunning } from '../lib/runtime'
import type { AssistantTurnState, StepView } from '../lib/types'
import { DelegationPanel } from './DelegationPanel'
import { IconChevronRight, IconLayers } from './Icons'
import { PlanPanel } from './PlanPanel'
import { ThinkingTimeline } from './ThinkingTimeline'

export function ExecutionProcess({ state, timeline }: { state: AssistantTurnState; timeline: StepView[] }) {
  // 用户主动打开后保持打开；否则只展示一行正在做什么，结束后便于读答案。
  const [open, setOpen] = useState(false)
  const running = isTurnRunning(state)
  const activity = executionActivity(state)
  if (!state.plan && !state.delegations.length && !timeline.length) return null
  return (
    <section className="process execution-process" data-testid="execution-process" data-open={open}>
      <button type="button" className="process__head" onClick={() => setOpen(!open)}
        aria-expanded={open} aria-label={open ? '收起执行过程' : '展开执行过程'}>
        <span className="process__chevron"><IconChevronRight size={13} /></span>
        <span className="process__title"><IconLayers size={14} />执行过程</span>
        {running && <span className="dot dot--live" />}
        <span className="execution-process__activity" role={running ? 'status' : undefined}>{activity.text}</span>
        <span className="process__count">{activity.progress}</span>
      </button>
      {open && <div className="execution-process__body">
        {state.plan && <PlanPanel plan={state.plan} streaming={running} />}
        {!!state.delegations.length && <DelegationPanel delegations={state.delegations} streaming={running} />}
        {!!timeline.length && <ThinkingTimeline steps={timeline}
          activeStep={running ? state.steps.at(-1)?.index ?? null : null} />}
      </div>}
    </section>
  )
}
