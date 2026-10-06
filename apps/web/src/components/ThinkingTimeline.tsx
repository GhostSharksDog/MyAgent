import type { StepView } from '../lib/types'
import { ToolCallCard } from './ToolCallCard'

/** 过程正文由统一 ExecutionProcess 控制收放，工具参数与结果仍能逐项展开。 */
export function ThinkingTimeline({ steps, activeStep }: { steps: StepView[]; activeStep: number | null }) {
  return (
    <div className="timeline">
      {steps.map((step) => (
        <div className="step" key={step.index} data-active={step.index === activeStep}>
          <span className="step__marker">{step.index}</span>
          <span className="step__label">第 {step.index} 步{step.toolCalls.length ? ' · ' + step.toolCalls.length + ' 次工具调用' : ''}</span>
          {step.text.trim() ? <div className="step__text">{step.text}</div>
            : !step.toolCalls.length ? <div className="step__text step__text--empty">模型本步没有输出文本</div> : null}
          {step.toolCalls.map((call) => <ToolCallCard key={call.id} call={call} />)}
        </div>
      ))}
    </div>
  )
}
