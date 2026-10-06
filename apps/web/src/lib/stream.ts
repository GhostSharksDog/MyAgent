/**
 * 事件流 → UI 状态的**纯归约器**。
 *
 * ============================================================
 * 一、为什么要单独抽一个纯函数
 * ============================================================
 *
 * 流式 UI 最容易写坏的地方，是"解析事件"和"setState"混在一起：
 * 一旦混起来，这段逻辑就只能靠手动点一遍页面来验证 —— 而流式时序
 * （token 跨 chunk、tool_result 先于 tool_call、final 覆盖 token、中途断流）
 * 恰恰是最难手动复现的部分。
 *
 * 这里把它做成 `(上一状态, 事件) => 新状态` 的纯函数后：
 *   - 可以脱离浏览器与网络，用一串录制好的事件直接断言结果（见 test/stream.test.mjs）；
 *   - React 侧只剩"收到事件就 apply 一次"，没有第二处状态逻辑。
 *
 * ============================================================
 * 二、token 为什么要累加，累加完还要被 final 覆盖
 * ============================================================
 *
 * `token` 是**增量**（模型每吐一小段就发一条），所以必须累加成整句才能显示。
 * 但累加结果和"最终答案"并不总是相等：
 *
 *   1. `token` 可能来自**中间步骤的过渡语**（"我先查一下你的简历"）。
 *      多步对话里，前面几步的文本是模型的思考旁白，不是答案。
 *      累加全部 token 会把过渡语和答案粘成一段，语义完全错乱。
 *   2. 后端对工具调用分片、或网络抖动时，可能丢帧/重帧（尤其断线重连场景）。
 *
 * 而后端的 `final` 事件是**内核自己认定的完整答案**（`loop.py` 里
 * `accumulator.content`，与非流式接口 `AgentRunResult.answer` 同一来源）。
 * 所以：token 负责"过程好看"，final 负责"结果正确"。
 * `final` 一到就以它为准覆盖，这是刻意的，不是多余的一次赋值。
 *
 * ============================================================
 * 三、步骤（step）是 Agent 与普通聊天机器人的分界线
 * ============================================================
 *
 * 一次回答被拆成若干 step，每个 step 里可能带若干工具调用。
 * 这个结构本身就值得展示：它回答的是"模型为了回答我，看了什么、算了什么"。
 * 因此状态模型不是 `{content}`，而是 `{steps: [{text, toolCalls}]}` ——
 * 数据结构的形状决定了 UI 能表达什么。
 */

import type {
  AgentEvent,
  AssistantTurnState,
  DelegationView,
  PlanPayload,
  StepView,
  StoppedReason,
  ToolCallView,
  TurnPhase,
} from './types'

// ============================================================
// 初始状态与状态转移
// ============================================================

export function emptyTurn(phase: TurnPhase = 'connecting'): AssistantTurnState {
  return {
    steps: [],
    answer: '',
    finalStep: null,
    usage: null,
    stepsUsed: 0,
    stoppedReason: null,
    error: null,
    phase,
    plan: null,
    delegations: [],
    contextTrimmed: false,
    contextTokens: 0,
  }
}

/** 取出（必要时创建）第 index 步。step 事件理论上先于该步的 token，但不依赖它。 */
function ensureStep(steps: StepView[], index: number): StepView[] {
  const existing = steps.findIndex((s) => s.index === index)
  if (existing !== -1) return steps
  return [...steps, { index, text: '', toolCalls: [] }]
}

/** 就地更新某一步（返回新数组，保持不可变语义）。 */
function patchStep(
  steps: StepView[],
  index: number,
  patch: (step: StepView) => StepView,
): StepView[] {
  return steps.map((s) => (s.index === index ? patch(s) : s))
}

/**
 * 归约一次事件。
 *
 * 未知事件类型一律**忽略**：后端的 `EventType` 将来会增长，
 * 旧前端遇到不认识的事件应当继续保持可用，而不是崩在 `switch` 的默认分支上。
 */
export function applyEvent(turn: AssistantTurnState, event: AgentEvent): AssistantTurnState {
  const stepIndex = typeof event.step === 'number' ? event.step : lastStepIndex(turn.steps)

  switch (event.type) {
    case 'start':
      return { ...turn, phase: 'thinking' }

    case 'step':
      return {
        ...turn,
        phase: 'thinking',
        steps: ensureStep(turn.steps, stepIndex),
      }

    case 'token': {
      const text = event.content ?? ''
      if (text === '') return turn
      return {
        ...turn,
        phase: 'streaming',
        steps: patchStep(ensureStep(turn.steps, stepIndex), stepIndex, (s) => ({
          ...s,
          text: s.text + text,
        })),
      }
    }

    case 'tool_call': {
      const call: ToolCallView = {
        id: `${stepIndex}-${turn.steps.find((s) => s.index === stepIndex)?.toolCalls.length ?? 0}`,
        name: event.tool_name ?? '(未命名工具)',
        args: event.tool_args ?? {},
        status: 'running',
        durationMs: null,
        truncated: false,
        output: '',
      }
      return {
        ...turn,
        // 发出 tool_call 说明这一步已经确定是"过程"而不是答案，光标回到思考态
        phase: 'thinking',
        steps: patchStep(ensureStep(turn.steps, stepIndex), stepIndex, (s) => ({
          ...s,
          toolCalls: [...s.toolCalls, call],
        })),
      }
    }

    case 'tool_result': {
      const target = findRunningToolCall(turn.steps, event.tool_name)
      const result = {
        status: (event.tool_ok === false ? 'failed' : 'ok') as ToolCallView['status'],
        durationMs: typeof event.duration_ms === 'number' ? event.duration_ms : null,
        truncated: event.truncated === true,
        output: event.content ?? '',
      }

      if (!target) {
        // 没有配对的 tool_call：不静默丢弃，补一张卡片出来。
        // 后端串行执行工具，正常不会发生；真发生了说明事件流被裁剪过，
        // 此时"看得见的异常"远好过"少了一张卡片却没人知道"。
        const orphan: ToolCallView = {
          id: `orphan-${stepIndex}-${turn.steps.length}`,
          name: event.tool_name ?? '(未知工具)',
          args: {},
          ...result,
        }
        return {
          ...turn,
          steps: patchStep(ensureStep(turn.steps, stepIndex), stepIndex, (s) => ({
            ...s,
            toolCalls: [...s.toolCalls, orphan],
          })),
        }
      }

      return {
        ...turn,
        steps: patchStep(turn.steps, target.stepIndex, (s) => ({
          ...s,
          toolCalls: s.toolCalls.map((c) => (c.id === target.call.id ? { ...c, ...result } : c)),
        })),
      }
    }

    case 'final':
      return {
        ...turn,
        // final 是权威答案 —— 覆盖 token 累加的结果（理由见文件头第二节）
        answer: event.content ?? '',
        finalStep: stepIndex,
        phase: 'done',
        error: null,
      }

    case 'error':
      return {
        ...turn,
        error: event.content ?? '未知错误',
        // 这里**不**改 phase，也不置 stoppedReason：
        // 步数耗尽/死循环也会发 ERROR，它们不是故障，
        // 权威判据是随后 DONE 事件里的 stopped_reason。
      }

    case 'done': {
      const reason = (event.stopped_reason ?? 'finished') as StoppedReason | string
      const failed = reason === 'error'
      return {
        ...turn,
        usage: event.usage ?? turn.usage,
        stepsUsed: typeof event.steps_used === 'number' ? event.steps_used : turn.stepsUsed,
        stoppedReason: reason,
        // 上下文被裁剪过就记下来，由 RunSummary 显示成一行提示。
        // 这是"它怎么忘了刚才说的"唯一的解释来源 —— 不显示的话，
        // 用户只能把它归因成"这个 Agent 记性不好"。
        contextTrimmed: event.context_trimmed === true,
        contextTokens:
          typeof event.context_tokens === 'number' ? event.context_tokens : turn.contextTokens,
        // 只有真的出错才进 error 相；预算终止进 done 相，由 UI 用警告色区分
        phase: failed ? 'error' : 'done',
      }
    }

    // ---------- 计划（Plan-and-Execute） ----------
    //
    // plan / plan_step / replan 三类事件都携带**完整计划快照**，
    // 所以这里一律直接整体替换 —— 不做 diff 合并。
    // 这不是偷懒：diff 要求前端自己维护一份可变状态并与后端保持一致，
    // 一旦漏掉某个字段的更新就会静默显示过期信息，而且极难复现。
    // 计划最多 5 步，快照的代价可以忽略。
    case 'plan':
    case 'plan_step':
    case 'replan': {
      const plan = readPlan(event)
      if (!plan) return turn
      return { ...turn, plan, phase: turn.answer ? turn.phase : 'thinking' }
    }

    // ---------- 多 Agent 协作 ----------
    case 'delegate': {
      const name = event.specialist ?? '(未命名专家)'
      // 同一位专家可能被派发多次（重试/不同子任务），每次都记一条 ——
      // UI 上它们是两次独立的工作，合并显示会掩盖"重试"这个事实。
      const delegation: DelegationView = {
        name,
        brief: event.content ?? '',
        status: 'running',
        output: '',
      }
      return { ...turn, delegations: [...turn.delegations, delegation] }
    }

    case 'delegate_result': {
      const name = event.specialist ?? '(未命名专家)'
      const status = event.tool_ok === false ? 'failed' : 'ok'
      const output = event.content ?? ''

      // 【配对方向与 tool_result 相反，这是刻意的】
      //
      // 工具用"从后往前找最近一个同名 running"（LIFO）：后端**串行**执行工具，
      // 第 1 个调用返回时第 2 个还没发生，所以最近的那个 running 正是它。
      //
      // 专家用"从前往后找第一个同名 running"（FIFO）：后端**并发**派发，
      // 所有 delegate 先一次性发出，结果按**完成顺序**回来 ——
      // 此时"最近一个"是最后派发的那位，把先到的结果配给它就错了。
      //
      // 已知的固有歧义：同名专家被派发两次时，事件里只有名字、没有序号，
      // 前端无法真正分辨谁是谁，只能按到达顺序填充。要彻底解决需要后端
      // 在 delegate / delegate_result 里带一个 correlation id。
      // 当前后端对专家名做了去重（见 SupervisorAgent._route），
      // 所以这个场景不会出现 —— 这里保留确定性行为作为兜底。
      const index = turn.delegations.findIndex((d) => d.name === name && d.status === 'running')

      if (index === -1) {
        // 没有配对的 delegate：补一条出来而不是丢弃。
        // 可见的异常远好过"少了一张卡片却没人知道"。
        return {
          ...turn,
          delegations: [...turn.delegations, { name, brief: '', status, output }],
        }
      }

      return {
        ...turn,
        delegations: turn.delegations.map((d, i) =>
          i === index ? { ...d, status, output } : d,
        ),
      }
    }

    default:
      return turn
  }
}

/**
 * 读取事件里的计划快照。
 *
 * 做一层结构校验而不是直接 `event.plan as PlanPayload`：
 * 事件类型放宽成 string 是为了向后兼容，代价就是拿到的 `plan` 字段
 * 在类型上是不受信的。非法结构直接当作"没有计划"处理，
 * 让 UI 退回普通渲染，而不是让整个归约过程崩掉。
 */
function readPlan(event: AgentEvent): PlanPayload | null {
  const plan = event.plan
  if (!plan || typeof plan !== 'object') return null
  if (typeof plan.goal !== 'string' || !Array.isArray(plan.steps)) return null
  return plan
}

function lastStepIndex(steps: StepView[]): number {
  const last = steps[steps.length - 1]
  return last ? last.index : 0
}

function findRunningToolCall(
  steps: StepView[],
  name: string | undefined,
): { stepIndex: number; call: ToolCallView } | null {
  // 从后往前找：模型可能在一两步里重复调用同一个工具，
  // 最近的那个 running 才是本次结果的归属。
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    const step = steps[i]
    if (!step) continue
    for (let j = step.toolCalls.length - 1; j >= 0; j -= 1) {
      const call = step.toolCalls[j]
      if (!call || call.status !== 'running') continue
      if (name === undefined || call.name === name) return { stepIndex: step.index, call }
    }
  }
  return null
}

// ============================================================
// 派生视图：把状态翻译成"该画什么"
// ============================================================

export type Indicator = 'connecting' | 'thinking' | 'tool' | null

export interface TurnView {
  /** 时间线上要渲染的步骤：排除"最终答案那一步"和"正在作为答案预览的那一步"。 */
  timeline: StepView[]
  /** 正在执行的工具（用于"正在执行工具 X…"状态与卡片微光）。 */
  runningTool: ToolCallView | null
  /** 已在答案区显示的内容（可能是流式预览，也可能是定稿答案）。 */
  answer: string
  /** answer 是否已经是权威答案（来自 final）。 */
  answerIsFinal: boolean
  /** 等待态文案。null 表示不该显示任何等待指示。 */
  indicator: Indicator
  toolCallCount: number
  /** 这一轮是否"有输出但最终失败"，UI 需要显著提示。 */
  isFailed: boolean
}

/**
 * 核心渲染决策：**当前正在生成的那一步，文本放哪儿？**
 *
 * 流式过程中我们并不知道当前这一步最后会不会去调工具：
 *   - 如果调了工具 → 这段文本是过渡语，应该落到"思考过程"时间线里；
 *   - 如果没调工具 → 它就是最终答案，应该留在答案区。
 *
 * 处理方式是"先按答案预览显示，一旦该步出现 tool_call 就自动降级到时间线"。
 * 用户看到的效果是：模型说话时先出现在答案区，一旦开始调工具，
 * 那句话原地滑进思考区、答案区重置为"思考中"—— 这恰好复现了 Agent 的真实决策过程。
 */
export function computeTurnView(turn: AssistantTurnState): TurnView {
  const last = turn.steps[turn.steps.length - 1]
  const runningTool = findRunningToolCall(turn.steps, undefined)?.call ?? null

  // 尚未定稿、且当前步还没发起任何工具调用 → 它在答案区当预览
  const previewStep =
    turn.answer === '' && last !== undefined && last.toolCalls.length === 0 ? last : null

  const timeline = turn.steps.filter(
    (s) => s.index !== turn.finalStep && (previewStep === null || s.index !== previewStep.index),
  )

  const answerIsFinal = turn.answer !== ''
  const answer = answerIsFinal ? turn.answer : (previewStep?.text ?? '')

  const indicator: Indicator =
    turn.phase === 'connecting'
      ? 'connecting'
      : runningTool
        ? 'tool'
        : turn.phase === 'thinking'
          ? 'thinking'
          : null

  const toolCallCount = turn.steps.reduce((n, s) => n + s.toolCalls.length, 0)

  return {
    timeline,
    runningTool,
    answer,
    answerIsFinal,
    indicator,
    toolCallCount,
    isFailed: turn.phase === 'error' && !answerIsFinal,
  }
}
