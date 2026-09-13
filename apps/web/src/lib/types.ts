/**
 * 前端侧的领域类型：**后端契约的镜像**。
 *
 * 这个文件存在的意义不是"多一层类型定义"，而是把契约固定在一处：
 * 后端的 `app/agent/events.py` 与 `app/api/schemas.py` 改了字段，
 * 只需要改这里，所有用到的地方都会被类型检查器指出来。
 * （更彻底的方案是从 OpenAPI 自动生成类型，属于后续计划。）
 *
 * 【关键约定】后端用 `model_dump_json(exclude_none=True)` 序列化事件，
 * 所以**所有 Optional 字段都可能在 JSON 里整个消失**，而不是给一个 null。
 * 因此这里把字段声明为可选，读取时一律给默认值 —— 不要写 `event.content!`。
 */

// ============================================================
// SSE 事件
// ============================================================

/** 终止原因。仅 DONE 事件携带。
 *
 *  刻意分开 `finished` 与其余三种：前三者是"可预期的预算终止"，
 *  只有 `error` 才是故障。UI 上也据此用不同颜色，而不是把所有
 *  非正常结束都画成红色 —— 那会让"步数耗尽"这种正常保护看起来像崩溃。 */
export type StoppedReason = 'finished' | 'max_steps' | 'loop_detected' | 'error'

/**
 * Agent 形态。三种形态针对不同的**任务结构**，不是"哪个更高级"：
 *
 *   react  想一步做一步     → 探索型任务（不知道下一步会看到什么）
 *   plan   先出完整计划再执行 → 结构型任务；计划对用户可见，可解释性最好
 *   multi  主管路由到多位专家 → 跨领域提问；各专家独立作答后由协调者取舍
 *
 * 类型的值是**从后端契约镜像**过来的。前端不硬编码"有哪些形态"的展示逻辑，
 * 而是拿 `/api/meta` 的 `agent_modes` 去渲染 —— 后端新增形态时前端无需改代码。
 * 这里的联合类型只用于编译期约束，不用于运行时枚举。
 */
export type AgentMode = 'react' | 'plan' | 'multi'

/** 每种形态的展示信息。键与后端 `agent_modes` 的取值对应。 */
export const AGENT_MODE_META: Record<AgentMode, { label: string; hint: string }> = {
  react: { label: '自动推理', hint: '边想边做，适合不确定下一步要查什么的探索型问题' },
  plan: { label: '先规划', hint: '先给出完整计划再逐步执行，过程对可见、便于中途纠偏' },
  multi: { label: '多专家', hint: '按专长路由到多位专家并发作答，适合跨领域的综合问题' },
}

/** 事件类型。与后端 `EventType` 一一对应。 */
export type AgentEventType =
  | 'start'
  | 'step'
  | 'token'
  | 'tool_call'
  | 'tool_result'
  | 'final'
  | 'error'
  | 'done'
  // Plan-and-Execute 专用（后端 app/agent/planning.py）
  | 'plan'
  | 'plan_step'
  | 'replan'
  // 多 Agent 专用（后端 app/agent/multi.py）
  | 'delegate'
  | 'delegate_result'

// ============================================================
// 计划（Plan-and-Execute）
// ============================================================

export type PlanStepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export interface PlanStepPayload {
  id: number
  description: string
  /** 这一步的完成标准。用于让用户判断"算不算做完了"。 */
  expected?: string
  status: PlanStepStatus
  /** 完成后的结论。注意它是**结论**而不是过程 —— 后端只把结论传给下一步。 */
  result?: string
  error?: string | null
}

export interface PlanPayload {
  goal: string
  steps: PlanStepPayload[]
  /** 为什么这样拆分。排查"拆得不对"时这是唯一的线索。 */
  reasoning?: string
}

// ============================================================
// 多 Agent 协作
// ============================================================

export interface DelegationView {
  /** 被派发的专家名。 */
  name: string
  /** 派发时后端给出的说明（来自专家描述）。 */
  brief: string
  status: 'running' | 'ok' | 'failed'
  /** 专家返回的结论（可能被截断）。 */
  output: string
}

export interface Usage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

/** 一条 Agent 事件。
 *
 *  `type` 放宽成 string：后端将来新增事件类型时，旧前端应当**忽略未知事件**
 *  而不是抛异常挂掉。这是流式协议最基本的向后兼容策略。 */
export interface AgentEvent {
  type: AgentEventType | (string & {})
  /** 第几步。start/token/tool_call/tool_result/final/step 都带。 */
  step?: number
  content?: string
  tool_name?: string
  tool_args?: Record<string, unknown>
  tool_ok?: boolean
  duration_ms?: number
  /** 工具观察结果是否被截断 —— 意味着模型看到的不是完整内容。 */
  truncated?: boolean
  usage?: Usage
  steps_used?: number
  stopped_reason?: StoppedReason | (string & {})
  /**
   * 计划快照。plan / plan_step / replan 三类事件都携带**完整快照**而非增量 diff。
   *
   * 这一点直接决定了前端实现：快照语义 = 直接替换状态，不需要自己维护
   * 一份可变计划并保证与后端一致（那是 bug 的温床）。计划最多 5 步，
   * 快照的传输代价可以忽略。
   */
  plan?: PlanPayload
  /** 被派发的专家名。delegate / delegate_result 携带。 */
  specialist?: string
}

// ============================================================
// 会话
// ============================================================

export interface SessionSummary {
  id: string
  title: string
  created_at: number
  updated_at: number
  turn_count: number
  total_tokens: number
}

export interface SessionTurn {
  role: 'user' | 'assistant'
  content: string
}

/**
 * 会话详情。
 *
 * 【注意】后端 `SessionDetail` **不含 turn_count**（列表项才有）。
 * 想显示轮次数得自己按 `turns.length / 2` 推。
 * 这是两个接口不一致的地方，前端只能适配。
 */
export interface SessionDetail {
  id: string
  title: string
  created_at: number
  updated_at: number
  total_tokens: number
  turns: SessionTurn[]
}

export interface SessionListResponse {
  sessions: SessionSummary[]
  /** `memory` 意味着多进程/多副本不共享，刷新或换实例就可能丢历史。 */
  backend: string
}

export interface DeleteResponse {
  deleted: boolean
}

// ============================================================
// 元信息
// ============================================================

/** GET /healthz（注意：不在 /api 下）。`tools` 这里只有名字。 */
export interface HealthStatus {
  status: string
  env: string
  llm_configured: boolean
  model: string
  tools: string[]
  session_backend: string
}

/** GET /api/meta */
export interface ApiMeta {
  service: string
  version: string
  env: string
  model: string
  max_steps: number
  tool_count: number
  session_backend: string
  /**
   * 后端支持的 Agent 形态。
   *
   * 声明为宽松的 `string[]` 而不是 `AgentMode[]`：这是**运行时发现**的数据，
   * 后端可能返回前端还不认识的新形态。收紧成联合类型会逼前端在解析处做断言，
   * 而断言一旦不成立就是运行时崩溃。宽松声明 + `AGENT_MODE_META` 过滤
   * 才能在"后端先行升级"时不把前端搞挂。
   */
  agent_modes?: string[]
}

/** GET /api/tools —— 这里是完整定义，含 JSON Schema。 */
export interface ToolInfo {
  name: string
  description: string
  parameters: Record<string, unknown>
}

// ============================================================
// UI 状态模型
// ============================================================

/** 一次工具调用的 UI 视图。由 tool_call + tool_result 两个事件合并而成。 */
export interface ToolCallView {
  id: string
  name: string
  args: Record<string, unknown>
  status: 'running' | 'ok' | 'failed'
  durationMs: number | null
  truncated: boolean
  /** 工具输出（或失败原因）。可能很长，UI 侧折叠显示。 */
  output: string
}

/** 一步（模型的一次思考 + 决策）。 */
export interface StepView {
  index: number
  /** 本步产出的文本。可能是过渡语（"我先查一下…"），也可能是最终答案。 */
  text: string
  toolCalls: ToolCallView[]
}

/** 一轮助手回复的流式生命周期状态。 */
export type TurnPhase =
  | 'connecting' // 请求已发出，还没收到 start
  | 'thinking' // 已进入某一步，还没吐出 token
  | 'streaming' // 正在吐 token
  | 'done'
  | 'error'
  | 'aborted'

export interface AssistantTurnState {
  /** 按 step 号递增的步骤列表 —— 这就是"时间线"的数据来源。 */
  steps: StepView[]
  /** `final` 事件给出的权威答案。为空表示这一轮没有产出最终答案。 */
  answer: string
  /** 产出最终答案的步号。渲染时用它把该步从时间线里排除，避免重复展示。 */
  finalStep: number | null
  usage: Usage | null
  stepsUsed: number
  stoppedReason: StoppedReason | string | null
  /** ERROR 事件的内容。注意它不等于"故障"：步数耗尽也会走这里。 */
  error: string | null
  phase: TurnPhase
  /**
   * 当前计划（Plan-and-Execute）。为 null 表示这一轮不是规划型 Agent。
   *
   * 后端每个计划事件都带完整快照，所以这里**直接整体替换**即可 ——
   * 不需要 diff 合并逻辑，也就不可能出现"前端状态与后端不一致"这类 bug。
   */
  plan: PlanPayload | null
  /** 被派发过的专家（多 Agent）。按派发顺序排列。 */
  delegations: DelegationView[]
}

/** 对话区的一条消息。 */
export type ChatItem =
  | { kind: 'user'; id: string; content: string; at: number }
  | {
      kind: 'assistant'
      id: string
      state: AssistantTurnState
      /** 来自会话历史而非本轮流式。后端的会话只存问答文本，不存工具调用过程，
       *  所以这里要显式告诉用户"这条没有过程可看"，而不是让人以为工具面板坏了。 */
      restored?: boolean
      at?: number
    }
