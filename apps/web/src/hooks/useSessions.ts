/**
 * 会话列表 Hook。
 *
 * 职责：维护"会话列表 + 当前选中会话 + 会话详情"这三份服务端状态，
 * 并把"切换会话时可能出现的竞态"处理掉。
 *
 * ============================================================
 * 竞态为什么要专门处理
 * ============================================================
 * 用户快速点 A → B 两个会话时，两个 `GET /api/sessions/{id}` 会并发飞行，
 * 而**先发的请求不一定先回来**（会话越大、Redis 越慢）。如果不管顺序，
 * 最终显示的可能偏偏是 A 的内容，而高亮的是 B —— 这是最容易被忽略、
 * 也最难复现的一类前端 bug。
 *
 * 做法：给每次请求打一个自增序号，只有"最新一次"的响应才允许写入状态。
 *
 * ============================================================
 * 为什么列表与详情分开请求
 * ============================================================
 * 这是后端刻意的接口设计（见 services/api/app/api/sessions.py 顶部注释）：
 * 列表不含对话内容，列出 20 个会话就是 20 次大 value 读取。
 * 前端必须顺着这个设计走 —— 只在真正打开某个会话时才拉详情。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  ApiError,
  createSession,
  deleteSession,
  getSession,
  listSessions,
} from '../lib/api'
import type { SessionDetail, SessionSummary } from '../lib/types'

export interface UseSessionsResult {
  sessions: SessionSummary[]
  /** `memory` / `redis` / `fake`。用于提示"多进程不共享"。 */
  backend: string
  activeId: string | null
  detail: SessionDetail | null
  loading: boolean
  /** 列表或详情加载失败时的中文提示（后端不可用时就是这里）。 */
  error: string | null
  refresh: () => Promise<void>
  select: (id: string) => Promise<void>
  create: () => Promise<void>
  remove: (id: string) => Promise<void>
  /** 取消选中，回到无状态模式（不传 session_id）。 */
  deselect: () => void
}

function describe(error: unknown): string {
  if (error instanceof ApiError) return error.detail
  if (error instanceof Error) return error.message
  return String(error)
}

export function useSessions(): UseSessionsResult {
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [backend, setBackend] = useState<string>('')
  const [activeId, setActiveId] = useState<string | null>(null)
  const [detail, setDetail] = useState<SessionDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 请求序号：只接受最新一次请求的结果（见文件头竞态说明）。
  // 列表与详情各用一个计数器 —— 共用一个会出事：发完消息触发的列表刷新
  // 会把仍在飞行中的"详情"请求判成过期，导致点开会话后对话区空白。
  const listSeq = useRef(0)
  const detailSeq = useRef(0)

  const refresh = useCallback(async () => {
    const seq = ++listSeq.current
    setLoading(true)
    try {
      const data = await listSessions(30)
      if (seq !== listSeq.current) return
      setSessions(data.sessions)
      setBackend(data.backend)
      setError(null)
    } catch (err) {
      if (seq !== listSeq.current) return
      setError(describe(err))
    } finally {
      if (seq === listSeq.current) setLoading(false)
    }
  }, [])

  const select = useCallback(async (id: string) => {
    const seq = ++detailSeq.current
    setActiveId(id)
    setLoading(true)
    try {
      const data = await getSession(id)
      if (seq !== detailSeq.current) return
      setDetail(data)
      setError(null)
    } catch (err) {
      if (seq !== detailSeq.current) return
      // 会话不存在（例如内存后端重启过）→ 清掉选中，并让列表回到真实状态
      setDetail(null)
      setError(describe(err))
      void refresh()
    } finally {
      if (seq === detailSeq.current) setLoading(false)
    }
  }, [refresh])

  const create = useCallback(async () => {
    try {
      const created = await createSession()
      // 抢占详情序号：即便此刻还有别的详情请求在飞，也不许它覆盖新会话
      detailSeq.current += 1
      setActiveId(created.id)
      // 新建的会话必然没有历史，直接构造空详情，省掉一次往返
      setDetail({
        id: created.id,
        title: created.title,
        created_at: created.created_at,
        updated_at: created.updated_at,
        total_tokens: created.total_tokens,
        turns: [],
      })
      setError(null)
      await refresh()
    } catch (err) {
      setError(describe(err))
    }
  }, [refresh])

  const remove = useCallback(
    async (id: string) => {
      try {
        await deleteSession(id)
        if (activeId === id) {
          detailSeq.current += 1
          setActiveId(null)
          setDetail(null)
        }
        await refresh()
      } catch (err) {
        setError(describe(err))
      }
    },
    [activeId, refresh],
  )

  const deselect = useCallback(() => {
    setActiveId(null)
    setDetail(null)
  }, [])

  // 首次挂载拉一次列表
  useEffect(() => {
    void refresh()
  }, [refresh])

  return {
    sessions,
    backend,
    activeId,
    detail,
    loading,
    error,
    refresh,
    select,
    create,
    remove,
    deselect,
  }
}
