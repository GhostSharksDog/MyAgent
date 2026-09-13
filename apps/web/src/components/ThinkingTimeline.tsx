/**
 * 思考过程时间线。
 *
 * ============================================================
 * 为什么"过渡语"必须和最终答案分开显示
 * ============================================================
 * ReAct 循环里，模型在每一步都会输出文本，但**大多数步骤的文本不是答案**：
 *
 *     第 1 步："我先查一下你的简历，看看和这个 JD 的匹配情况。" → 然后调 read_resume
 *     第 2 步："再确认一下岗位要求。"                          → 然后调 search_jobs
 *     第 3 步：真正的完整回答                                   → 结束循环（无工具调用）
 *
 * 如果把它们按到达顺序拼在一起（很多简易实现就是这么做的），用户会看到
 * 一段"先说要做 A，然后是一堆工具输出，最后才给答案"的混乱文本，
 * 而中间那些"我打算……"的句子会被误读成结论。
 *
 * 这里把非最终步骤的文本收进这条时间线，并且用更低的对比度、
 * 斜体、左侧导轨来弱化它们 —— 视觉上明确"这是过程"。
 * 最终答案则单独渲染在下方（见 AssistantTurn）。
 *
 * 时间线的另一个作用是**顺序**：工具调用按 step 分组、按发生顺序排列，
 * 用户能看出"它先查了简历，再查岗位，中间还失败重启过一次"。
 * 这是纯结果视图给不了的信息。
 */

import { useState } from 'react'

import type { StepView } from '../lib/types'
import { IconChevronRight, IconLayers } from './Icons'
import { ToolCallCard } from './ToolCallCard'

export interface ThinkingTimelineProps {
  steps: StepView[]
  /** 当前正在进行的步号（用于高亮）。 */
  activeStep: number | null
  /** 这一轮是否还在流式（决定默认展开与文案）。 */
  streaming: boolean
}

export function ThinkingTimeline({ steps, activeStep, streaming }: ThinkingTimelineProps) {
  // 流式过程中默认展开（用户想看它正在干什么）；
  // 完成后默认收起（读答案时不需要过程噪声）。用户手动展开后就不再自动改回。
  const [override, setOverride] = useState<boolean | null>(null)
  const open = override ?? streaming

  const toolCount = steps.reduce((sum, step) => sum + step.toolCalls.length, 0)

  return (
    <div className="process" data-open={open}>
      <button
        type="button"
        className="process__head"
        onClick={() => setOverride(!open)}
        aria-expanded={open}
      >
        <span className="process__chevron">
          <IconChevronRight size={12} />
        </span>
        <span className="process__title">
          <IconLayers size={13} />
          {streaming ? '思考过程（进行中）' : '思考过程'}
        </span>
        <span className="process__count">
          {steps.length} 步
          {toolCount > 0 ? ` · ${toolCount} 次工具调用` : ''}
        </span>
      </button>

      {open ? (
        <div className="timeline">
          {steps.map((step) => (
            <div className="step" key={step.index} data-active={step.index === activeStep}>
              <span className="step__marker">{step.index}</span>
              <span className="step__label">
                第 {step.index} 步
                {step.toolCalls.length > 0 ? ` · 发起 ${step.toolCalls.length} 次工具调用` : ''}
              </span>

              {step.text.trim() ? (
                <div className="step__text">{step.text}</div>
              ) : step.toolCalls.length === 0 ? (
                <div className="step__text step__text--empty">（模型本步没有输出文本）</div>
              ) : null}

              {step.toolCalls.map((call) => (
                <ToolCallCard key={call.id} call={call} />
              ))}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}
