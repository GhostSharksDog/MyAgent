/**
 * 计划面板 —— Plan-and-Execute 的可解释性出口。
 *
 * ============================================================
 * 为什么"把计划画出来"是这个组件存在的全部理由
 * ============================================================
 * ReAct 是"想一步做一步"，它的思考过程只能靠**事后**从工具调用反推。
 * Plan-and-Execute 的区别在于它**先产出一份完整的计划** ——
 * 计划是人能看懂的中间产物，它把 Agent 的意图在动手之前就摊开了。
 *
 * 这带来两个只有规划型 Agent 才有的好处：
 *   1. 用户可以在**执行途中**发现方向错了（而不是等它做完）
 *   2. 出错时能立刻定位到"是哪一步的问题"，而不是面对一坨工具调用
 *
 * 所以这个面板不是装饰：它是把"可解释性"这个抽象优势变成可见的东西。
 *
 * ============================================================
 * 状态语义（与后端 PlanStepStatus 一一对应）
 * ============================================================
 *   pending  等待执行
 *   running  正在执行（有动画）
 *   done     已完成 —— 展示它的**结论**而不是过程
 *   failed   失败 —— 展示失败原因，并说明这可能触发重规划
 *   skipped  被跳过（通常是 token 预算耗尽）
 *
 * `skipped` 刻意用中性色而不是警告色：预算保护生效是**设计如此**，
 * 不是异常。把它画成红色会让用户以为系统坏了。
 */

import { useState } from 'react'
import type { ReactNode } from 'react'

import type { PlanPayload, PlanStepPayload, PlanStepStatus } from '../lib/types'
import { IconChevronRight, IconSpinner } from './Icons'

const STATUS_LABEL: Record<PlanStepStatus, string> = {
  pending: '等待',
  running: '执行中',
  done: '完成',
  failed: '失败',
  skipped: '已跳过',
}

function StatusDot({ status }: { status: PlanStepStatus }): ReactNode {
  if (status === 'running') {
    return (
      <span className="plan__dot plan__dot--running" title={STATUS_LABEL[status]}>
        <IconSpinner size={11} />
      </span>
    )
  }
  return <span className={`plan__dot plan__dot--${status}`} title={STATUS_LABEL[status]} />
}

export interface PlanPanelProps {
  plan: PlanPayload
  /** 仍在流式中：用于决定标题文案与是否显示进度。 */
  streaming: boolean
}

export function PlanPanel({ plan, streaming }: PlanPanelProps) {
  const [open, setOpen] = useState(true)

  const done = plan.steps.filter((s) => s.status === 'done').length
  const failed = plan.steps.filter((s) => s.status === 'failed').length
  const total = plan.steps.length

  return (
    <section className="plan" aria-label="执行计划">
      <button
        type="button"
        className="plan__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className={`plan__chevron${open ? ' plan__chevron--open' : ''}`}>
          <IconChevronRight size={13} />
        </span>
        <span className="plan__title">执行计划</span>
        <span className="plan__goal" title={plan.goal}>
          {plan.goal}
        </span>
        <span className="plan__progress">
          {done}/{total} 步
          {failed > 0 ? <span className="plan__failed-count">· {failed} 步失败</span> : null}
          {streaming ? <span className="plan__live">执行中</span> : null}
        </span>
      </button>

      {open ? (
        <div className="plan__body">
          {/* reasoning：为什么这样拆。它是排查"计划不合理"的唯一线索，
              所以默认展示而不是藏在二级折叠里。 */}
          {plan.reasoning ? (
            <div className="plan__reasoning">
              <span className="plan__reasoning-label">拆分理由</span>
              {plan.reasoning}
            </div>
          ) : null}

          <ol className="plan__steps">
            {plan.steps.map((step) => (
              <PlanStepRow key={`${step.id}-${step.description}`} step={step} />
            ))}
          </ol>
        </div>
      ) : null}
    </section>
  )
}

function PlanStepRow({ step }: { step: PlanStepPayload }) {
  return (
    <li className="plan__step" data-status={step.status}>
      <StatusDot status={step.status} />
      <span className="plan__step-id">{step.id}</span>

      <div className="plan__step-main">
        <div className="plan__step-desc">{step.description}</div>

        {/* 完成标准：让用户能自己判断"算不算做完了"，
            而不是只能相信那个状态点。 */}
        {step.expected && step.status === 'pending' ? (
          <div className="plan__step-expected">完成标准：{step.expected}</div>
        ) : null}

        {/* 已完成 → 展示结论。注意这是**结论**而不是过程：
            后端只把结论传给下一步，所以这里展示的也正是模型看到的。 */}
        {step.status === 'done' && step.result ? (
          <div className="plan__step-result">{step.result}</div>
        ) : null}

        {step.status === 'failed' ? (
          <div className="plan__step-error">
            {step.error || '执行失败'}
            {/* 失败会触发重规划（若启用）。把这条因果说出来，
                否则用户会困惑"为什么计划里突然多了几步"。 */}
            <div className="plan__step-hint">
              该步骤失败可能触发计划修订，后续步骤会据此调整。
            </div>
          </div>
        ) : null}

        {step.status === 'skipped' ? (
          <div className="plan__step-skipped-note">
            {step.error || '已被跳过'}（这是预算保护，不是故障）
          </div>
        ) : null}
      </div>
    </li>
  )
}
