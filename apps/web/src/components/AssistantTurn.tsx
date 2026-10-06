/**
 * 一轮助手回复的完整渲染：思考过程 → 最终答案 → 本轮用量。
 *
 * ============================================================
 * 关于"边流边渲染"的一个决策
 * ============================================================
 * 流式过程中，当前这一步的文本会先出现在**答案区**（用户想马上看到模型在说话），
 * 一旦这一步发起了工具调用，它就会被自动降级到**思考区**（因为它其实是过渡语）。
 *
 * 这个判断需要"知道未来"，而流式恰恰是不知道未来的。做法是：
 * 先按最可能的意图渲染（答案），拿到新证据再纠正（降级）。
 * 用户看到的效果就是模型的那句话"滑"进了过程区 —— 这个动作本身
 * 比任何文案都更能说明 Agent 的工作方式。
 *
 * 具体规则在 lib/stream.ts 的 computeTurnView 里，是可单测的纯函数；
 * 这里只负责把派生出的视图画出来。
 */

import { computeTurnView } from '../lib/stream'
import { formatCompact, formatNumber } from '../lib/format'
import type { AssistantTurnState, ChatItem } from '../lib/types'
import type { ReactNode } from 'react'
import { DelegationPanel } from './DelegationPanel'
import { IconAlert, IconCoins, IconInfo, IconLayers, IconScissors } from './Icons'
import { Markdown } from './Markdown'
import { PlanPanel } from './PlanPanel'
import { ThinkingTimeline } from './ThinkingTimeline'

type AssistantItem = Extract<ChatItem, { kind: 'assistant' }>

/** 终止原因 → 人类可读的说明。
 *  `finished` 之外都不是"崩溃"，而是**可预期的预算终止**（后端的分类刻意区分了这两者）。 */
function describeStop(reason: string | null): { text: string; tone: 'ok' | 'warn' | 'danger' } {
  switch (reason) {
    case 'finished':
      return { text: '正常结束', tone: 'ok' }
    case 'max_steps':
      return { text: '达到步数上限', tone: 'warn' }
    case 'loop_detected':
      return { text: '检测到重复调用，已中止', tone: 'warn' }
    case 'error':
      return { text: '执行出错', tone: 'danger' }
    case null:
      return { text: '未收到结束事件', tone: 'warn' }
    default:
      return { text: String(reason), tone: 'warn' }
  }
}

function isStreaming(state: AssistantTurnState): boolean {
  return state.phase === 'connecting' || state.phase === 'thinking' || state.phase === 'streaming'
}

function Indicator({ kind, toolName }: { kind: string; toolName?: string }): ReactNode {
  const label =
    kind === 'connecting'
      ? '正在连接后端…'
      : kind === 'tool'
        ? `正在执行工具 ${toolName ?? ''}…`
        : '思考中…'
  return (
    <div className="thinking">
      <span className="thinking__dots">
        <span />
        <span />
        <span />
      </span>
      <span>{label}</span>
    </div>
  )
}

function RunSummary({
  state,
  toolCount,
  streaming,
}: {
  state: AssistantTurnState
  toolCount: number
  streaming: boolean
}) {
  const stop = streaming
    ? { text: '进行中', tone: 'live' as const }
    : describeStop(state.stoppedReason)
  const usage = state.usage

  return (
    <div className="runmeta">
      <span className="runmeta__item">
        <IconLayers size={11} />
        <span>{state.stepsUsed || state.steps.length} 步</span>
      </span>
      {toolCount > 0 ? (
        <span className="runmeta__item">
          <span>·</span>
          <span>{toolCount} 次工具调用</span>
        </span>
      ) : null}
      {usage ? (
        <span className="runmeta__item" title="输入 / 输出 / 合计 token">
          <IconCoins size={11} />
          <span>
            {formatCompact(usage.prompt_tokens)} / {formatCompact(usage.completion_tokens)} /{' '}
            {formatNumber(usage.total_tokens)} tokens
          </span>
        </span>
      ) : null}
      <span className="runmeta__item">
        <span
          className={
            stop.tone === 'ok'
              ? 'dot dot--ok'
              : stop.tone === 'live'
                ? 'dot dot--live'
                : stop.tone === 'danger'
                  ? 'dot dot--danger'
                  : 'dot dot--warn'
          }
        />
        <span>{stop.text}</span>
      </span>
      {/* 上下文被裁剪过必须显示出来。
          它是"它怎么忘了刚才说的"唯一的解释来源 —— 不显示的话，
          用户只能把这个现象归因成"这个 Agent 记性不好"。 */}
      {state.contextTrimmed ? (
        <span
          className="runmeta__item"
          style={{ color: 'var(--warn)' }}
          title={
            '本轮上下文超过预算，最早的对话轮次已被丢弃' +
            (state.contextTokens ? `（裁剪后约 ${state.contextTokens} token）` : '') +
            '。可在设置里调大 AGENT_CONTEXT_TOKEN_BUDGET。'
          }
        >
          <IconScissors size={11} />
          <span>上下文已裁剪</span>
        </span>
      ) : null}
    </div>
  )
}

export interface AssistantTurnProps {
  item: AssistantItem
  /** 是否显示耗时/token 元信息（通用设置里的开关，默认开）。 */
  showMeta?: boolean
}

export function AssistantTurn({ item, showMeta = true }: AssistantTurnProps) {
  const { state } = item
  const view = computeTurnView(state)
  const streaming = isStreaming(state)
  const activeStep = streaming ? (state.steps[state.steps.length - 1]?.index ?? null) : null

  return (
    <article className="turn">
      <header className="turn__head">
        <span className="turn__avatar">JP</span>
        <span>Legacy Agent</span>
        {streaming ? <span className="dot dot--live" /> : null}
        {item.restored ? (
          <span title="会话历史里只保存了问答文本，工具调用过程不会被持久化">
            来自会话历史 · 不含工具调用过程
          </span>
        ) : null}
      </header>

      {/* 渲染顺序 = 抽象层次由高到低：
       *   计划（整体意图） → 专家（职责分工） → 时间线（具体动作） → 答案
       * 这个顺序让用户先看到"它打算怎么做"，再看"它做了什么"，
       * 最后才是结论 —— 与人类理解一个复杂任务的过程一致。 */}
      {state.plan ? <PlanPanel plan={state.plan} streaming={streaming} /> : null}

      {state.delegations.length > 0 ? (
        <DelegationPanel delegations={state.delegations} streaming={streaming} />
      ) : null}

      {view.timeline.length > 0 ? (
        <ThinkingTimeline steps={view.timeline} activeStep={activeStep} streaming={streaming} />
      ) : null}

      {/* 出错的轮次：把 error 事件的内容显著地摆出来。
          注意"步数耗尽 / 死循环"也会走这里 —— 它们不是崩溃，
          而是保护机制生效，所以用警告色而不是报错色区分。 */}
      {state.error ? (
        <div
          className={
            state.phase === 'error' ? 'notice notice--error' : 'notice notice--warn'
          }
          role="alert"
        >
          <span className="notice__icon">
            <IconAlert size={15} />
          </span>
          <span className="notice__body">
            <span className="notice__title">
              {state.stoppedReason === 'max_steps'
                ? '已达到单步最大步数限制'
                : state.stoppedReason === 'loop_detected'
                  ? '检测到重复的工具调用，已主动中止'
                  : state.phase === 'error'
                    ? '本轮执行出错'
                    : '本轮未正常结束'}
            </span>
            <span className="notice__text">{state.error}</span>
          </span>
        </div>
      ) : null}

      <div className={`answer${view.answerIsFinal ? '' : ' answer--partial'}`}>
        {view.answer ? (
          <>
            <div className="divider-label">
              {view.answerIsFinal ? '最终答案' : '生成中'}
            </div>
            <div className="answer__body">
              <Markdown source={view.answer} />
              {!view.answerIsFinal && streaming ? <span className="caret" /> : null}
            </div>
          </>
        ) : view.indicator ? (
          <Indicator kind={view.indicator} toolName={view.runningTool?.name} />
        ) : item.restored ? null : !state.error ? (
          <div className="answer__body muted">
            <span className="notice__icon" style={{ marginRight: 6 }}>
              <IconInfo size={13} />
            </span>
            本轮没有产出最终答案
            {state.phase === 'aborted' ? '（已中断，已完成的部分见上方思考过程）' : '。'}
          </div>
        ) : null}
      </div>

      {item.restored || !showMeta ? null : (
        <RunSummary state={state} toolCount={view.toolCallCount} streaming={streaming} />
      )}
    </article>
  )
}
