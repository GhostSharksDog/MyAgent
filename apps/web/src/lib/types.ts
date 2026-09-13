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

/** 终止原因。仅 DONE 事件携带。
 *
 *  刻意分开 `finished` 与其余三种：前三者是"可预期的预算终止"，
 *  只有 `error` 才是故障。UI 上也据此用不同颜色，而不是把所有
 *  非正常结束都画成红色 —— 那会让"步数耗尽"这种正常保护看起来像崩溃。 */
export type StoppedReason = 'finished' | 'max_steps' | 'loop_detected' | 'error'

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
