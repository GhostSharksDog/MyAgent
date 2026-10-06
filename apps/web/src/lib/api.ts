/**
 * 后端 HTTP 客户端：**唯一的出网入口**。
 *
 * 设计意图：
 *   1. 所有组件都不直接 `fetch`。接口变了只改这一处，也让"哪些请求会发出"
 *      可以一眼看全。
 *   2. 错误统一成 `ApiError`，并且**尽量把后端的中文错误信息原样带出来**。
 *      后端在 `_resolve` 里为"会话不存在"写了很具体的提示
 *      （"请重新创建会话，或改用无状态模式"），前端自己编一句
 *      "请求失败"就把这条有用信息丢了 —— 这类信息在本项目里是刻意写的。
 *   3. `/api/chat/stream` 只负责**发起请求并交回 Response**，
 *      解析在 lib/sse.ts。分开的理由：这一层只关心 HTTP 与错误状态码。
 */

import { accessHeaders } from './access'
import type {
  AgentMode,
  FileApprovalDecision,
  ApiMeta,
  DeleteResponse,
  HealthStatus,
  SessionDetail,
  SessionListResponse,
  SessionSummary,
  ToolInfo,
} from './types'

/**
 * 后端基础地址。
 *   - 空字符串（默认）：请求走相对路径，由 vite 代理（开发）或同源网关（生产）转发；
 *   - 部署到别的域名时用 `VITE_API_BASE=https://api.example.com` 覆盖。
 */
const API_BASE: string = import.meta.env.VITE_API_BASE ?? ''

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

/** 带上后端错误正文的异常。`status === 0` 表示压根没连上。 */
export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }

  /** 连接层失败（后端没启动、端口不对、被防火墙挡了）。 */
  get isOffline(): boolean {
    return this.status === 0
  }
}

/** 把响应体里的错误信息抠出来。兼容 FastAPI 的两种形态：
 *  - HTTPException → `{"detail": "中文提示"}`
 *  - 校验错误(422) → `{"detail": [{"loc": [...], "msg": "..."}]}` */
async function readErrorDetail(response: Response): Promise<string> {
  let raw = ''
  try {
    raw = await response.text()
  } catch {
    return `HTTP ${response.status}`
  }
  if (!raw.trim()) return `HTTP ${response.status} ${response.statusText}`.trim()

  try {
    const parsed: unknown = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
      const detail = (parsed as { detail: unknown }).detail
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail)) {
        return detail
          .map((item) => {
            const entry = item as { loc?: unknown[]; msg?: unknown }
            const loc = Array.isArray(entry.loc) ? entry.loc.join('.') : ''
            return loc ? `${loc}: ${String(entry.msg)}` : String(entry.msg)
          })
          .join('；')
      }
    }
    return raw.slice(0, 500)
  } catch {
    return raw.slice(0, 500)
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
}

/** 发一个 JSON 请求并解析响应。所有错误都收敛成 ApiError。 */
export async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, signal } = options
  // 访问密钥放在**请求头**里（不放 URL、不放 Cookie，理由见 lib/access.ts）。
  // 每次都现读：用户刚在设置里填完密钥，不该等到刷新页面才生效。
  const headers: Record<string, string> = { ...accessHeaders() }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(apiUrl(path), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    })
  } catch (error) {
    if (signal?.aborted) throw error
    // fetch 只在网络层失败时 reject：后端没起来就是这里
    throw new ApiError(0, `无法连接后端服务（${apiUrl(path)}）。请确认它已启动。`)
  }

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorDetail(response))
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

// ============================================================
// 元信息
// ============================================================

/** 健康检查。注意它在**根路径**，不在 /api 下。 */
export function fetchHealth(signal?: AbortSignal): Promise<HealthStatus> {
  return requestJson<HealthStatus>('/healthz', { signal })
}

export function fetchMeta(signal?: AbortSignal): Promise<ApiMeta> {
  return requestJson<ApiMeta>('/api/meta', { signal })
}

export function fetchTools(signal?: AbortSignal): Promise<ToolInfo[]> {
  return requestJson<ToolInfo[]>('/api/tools', { signal })
}

export async function decideFileApproval(runId: string, id: string, decision: FileApprovalDecision,
  signal?: AbortSignal): Promise<{ status: 'approved' | 'rejected' }> {
  const result = await requestJson<{ status?: unknown }>(`/api/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(id)}`,
    { method: 'POST', body: { decision }, signal })
  if (!result || (result.status !== 'approved' && result.status !== 'rejected')) {
    throw new ApiError(502, '服务未返回有效确认状态，请等待执行事件或检查服务日志，不能据此认定写入成功。')
  }
  return { status: result.status }
}

// ============================================================
// 会话
// ============================================================

export function listSessions(limit = 30, signal?: AbortSignal): Promise<SessionListResponse> {
  return requestJson<SessionListResponse>(`/api/sessions?limit=${limit}`, { signal })
}

export function createSession(signal?: AbortSignal): Promise<SessionSummary> {
  return requestJson<SessionSummary>('/api/sessions', { method: 'POST', signal })
}

export function getSession(id: string, signal?: AbortSignal): Promise<SessionDetail> {
  return requestJson<SessionDetail>(`/api/sessions/${encodeURIComponent(id)}`, { signal })
}

export function deleteSession(id: string, signal?: AbortSignal): Promise<DeleteResponse> {
  return requestJson<DeleteResponse>(`/api/sessions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    signal,
  })
}

// ============================================================
// 流式对话
// ============================================================

export interface ChatStreamRequest {
  message: string
  sessionId?: string | null
  /** Agent 形态。不传由后端用默认值（react）。 */
  mode?: AgentMode
}

/**
 * 发起流式对话，把 `Response` 交回给调用方去消费 SSE。
 *
 * 【关键点：错误有两种完全不同的形态】
 *   1. **流开始之前**出错（会话不存在 → 404、请求体不合法 → 422）：
 *      此时还是普通 JSON 响应，必须检查 `response.ok`。
 *   2. **流开始之后**出错：HTTP 状态码已经发出去了（200），
 *      后端只能以内联的 `error` 事件告知（见 routes.py 的 try/except）。
 *      这一种由 lib/sse.ts 解析出事件、由 UI 展示。
 *
 * 只按"HTTP 200 就万事大吉"写，会让 404 的会话静默地什么都不显示。
 */
export async function openChatStream(
  payload: ChatStreamRequest,
  signal?: AbortSignal,
): Promise<Response> {
  const body: { message: string; session_id?: string; mode?: string } = {
    message: payload.message,
  }
  // 不传 session_id 就是无状态模式（后端会忽略 history），这是契约里的合法用法
  if (payload.sessionId) body.session_id = payload.sessionId
  // 不传 mode 时后端用默认值 —— 前端不必硬编码"默认是 react"这个知识
  if (payload.mode) body.mode = payload.mode

  let response: Response
  try {
    response = await fetch(apiUrl('/api/chat/stream'), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...accessHeaders(),
      },
      body: JSON.stringify(body),
      signal,
    })
  } catch (error) {
    if (signal?.aborted) throw error
    throw new ApiError(0, '无法连接后端服务。请确认 `.\scripts\dev.ps1 serve` 已启动。')
  }

  if (!response.ok) {
    throw new ApiError(response.status, await readErrorDetail(response))
  }
  if (!response.body) {
    throw new ApiError(response.status, '后端返回了空响应体，无法读取 SSE 流。')
  }
  return response
}
