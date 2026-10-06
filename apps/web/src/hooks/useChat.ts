/**
 * 对话引擎 Hook：把"发一条消息"到"UI 状态滚动更新"的全过程收敛在一处。
 *
 * ============================================================
 * 为什么这个 Hook 不长在组件里
 * ============================================================
 * 一次流式对话要同时管理：AbortController、SSE 消费循环、逐事件的状态归约、
 * 异常分类（用户主动中断 vs 后端错误 vs 网络断开）、以及"结束时刷新会话列表"。
 * 这些逻辑放进任何组件都会把组件变成状态机，而它们的**生命周期**
 * 又必须和整棵对话区一致（切换会话时不能串流）。
 *
 * 拆出来之后：
 *   - 组件只读 `items` / `isStreaming` 并调用 `send`;
 *   - 事件到状态的映射是纯函数（lib/stream.ts），可以单独测。
 *
 * ============================================================
 * 中断为什么必须显式做
 * ============================================================
 * `AbortController` 不只是让 UI 停止更新：它会**真的断掉 HTTP 连接**。
 * 后端在 `event_generator` 里检测到客户端断开就会停止产出，
 * 从而省掉后续的模型调用与工具执行。前端"停止生成"按钮因此也是**省钱的按钮**，
 * 而不是一个纯视觉的开关。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, openChatStream } from '../lib/api'
import { streamAgentEvents } from '../lib/sse'
import { applyEvent, emptyTurn } from '../lib/stream'
import type { AgentMode, AssistantTurnState, ChatItem, SessionTurn } from '../lib/types'

function createId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // 老浏览器/非安全上下文的兜底。id 只用于 React key，不需要全局唯一。
  return `id-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** 就地替换某条助手消息的状态。 */
function patchAssistant(
  items: ChatItem[],
  id: string,
  patch: (state: AssistantTurnState) => AssistantTurnState,
): ChatItem[] {
  return items.map((item) =>
    item.kind === 'assistant' && item.id === id ? { ...item, state: patch(item.state) } : item,
  )
}

export interface UseChatOptions {
  /** 一轮结束后回调（用于刷新侧边栏的标题/轮次/token 统计）。 */
  onTurnSettled?: () => void
}

export interface UseChatResult {
  items: ChatItem[]
  isStreaming: boolean
  /** 顶部/底部的临时提示（例如"已停止生成"）。 */
  notice: string | null
  send: (text: string, sessionId: string | null, mode?: AgentMode) => Promise<void>
  abort: () => void
  clear: () => void
  /** 载入某个会话的历史（来自 GET /api/sessions/{id}）。 */
  loadHistory: (turns: SessionTurn[]) => void
}

export function useChat(options: UseChatOptions = {}): UseChatResult {
  const [items, setItems] = useState<ChatItem[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const controllerRef = useRef<AbortController | null>(null)
  const streamingRef = useRef(false)
  const aliveRef = useRef(true)
  // onTurnSettled 放进 ref：它是调用方每次渲染新建的函数，
  // 直接进 useCallback 依赖会让 send 的引用每帧都变，进而让下游 memo 全部失效。
  const settledRef = useRef(options.onTurnSettled)
  settledRef.current = options.onTurnSettled

  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
      // 组件卸载（或切页）时断流：否则后台还在跑 Agent，用户却看不到任何反馈
      controllerRef.current?.abort()
    }
  }, [])

  const abort = useCallback(() => {
    controllerRef.current?.abort()
  }, [])

  const clear = useCallback(() => {
    const previous = controllerRef.current
    controllerRef.current = null
    streamingRef.current = false
    setIsStreaming(false)
    previous?.abort()
    setItems([])
    setNotice(null)
  }, [])

  const loadHistory = useCallback((turns: SessionTurn[]) => {
    const previous = controllerRef.current
    controllerRef.current = null
    streamingRef.current = false
    setIsStreaming(false)
    previous?.abort()
    setNotice(null)

    const restored: ChatItem[] = turns
      .filter((turn) => turn.content.trim() !== '')
      .map((turn, index) =>
        turn.role === 'user'
          ? { kind: 'user', id: `history-${index}`, content: turn.content, at: 0 }
          : {
              kind: 'assistant',
              id: `history-${index}`,
              restored: true,
              state: {
                ...emptyTurn('done'),
                answer: turn.content,
                finalStep: 0,
                stoppedReason: 'finished',
              },
            },
      )
    setItems(restored)
  }, [])

  const send = useCallback(
    async (text: string, sessionId: string | null, mode?: AgentMode) => {
      const message = text.trim()
      if (message === '' || streamingRef.current) return

      const assistantId = createId()
      const userItem: ChatItem = {
        kind: 'user',
        id: createId(),
        content: message,
        at: Date.now(),
      }
      setItems((prev) => [
        ...prev,
        userItem,
        { kind: 'assistant', id: assistantId, state: emptyTurn('connecting') },
      ])

      const controller = new AbortController()
      controllerRef.current = controller
      streamingRef.current = true
      setIsStreaming(true)
      setNotice(null)

      let sawDone = false

      try {
        const response = await openChatStream({ message, sessionId, mode }, controller.signal)
        if (controllerRef.current !== controller || !aliveRef.current) {
          await response.body?.cancel()
          return
        }

        for await (const event of streamAgentEvents(response, { signal: controller.signal })) {
          // 切换历史或清空后，旧流的迟到事件不能修改当前轮次。
          if (controllerRef.current !== controller || !aliveRef.current) break
          if (event.type === 'done') sawDone = true
          setItems((prev) => patchAssistant(prev, assistantId, (state) => applyEvent(state, event)))
        }

        // 流正常收尾但没有 done：说明连接被中间层掐断或后端提前退出。
        // 这种情况必须显式告知 —— 否则用户会盯着一句"没写完的话"以为模型就这水平。
        if (!sawDone && controllerRef.current === controller && aliveRef.current) {
          setItems((prev) =>
            patchAssistant(prev, assistantId, (state) =>
              state.phase === 'done' || state.phase === 'error'
                ? state
                : {
                    ...state,
                    phase: 'aborted',
                    error: state.error ?? '连接在收到结束事件（done）之前中断，本轮结果可能不完整。',
                  },
            ),
          )
        }
      } catch (error) {
        if (controllerRef.current !== controller || !aliveRef.current) return
        if (controller.signal.aborted) {
          setItems((prev) =>
            patchAssistant(prev, assistantId, (state) => ({ ...state, phase: 'aborted' })),
          )
          setNotice('已停止生成（连接已断开，后端会停止后续步骤）')
        } else {
          const detail =
            error instanceof ApiError
              ? error.detail
              : error instanceof Error
                ? error.message
                : String(error)
          setItems((prev) =>
            patchAssistant(prev, assistantId, (state) => ({
              ...state,
              phase: 'error',
              error: detail,
            })),
          )
        }
      } finally {
        // 旧请求可能在新请求开始以后才收到取消错误，不能清掉新控制器。
        if (controllerRef.current === controller) {
          streamingRef.current = false
          controllerRef.current = null
          if (aliveRef.current) {
            setIsStreaming(false)
            settledRef.current?.()
          }
        }
      }
    },
    [],
  )

  return { items, isStreaming, notice, send, abort, clear, loadHistory }
}
