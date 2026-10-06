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
  /** 服务端的实际存储后端，例如 memory / redis / sqlite。 */
  backend: string
  activeId: string | null
  detail: SessionDetail | null
  loading: boolean
  /** 新建、切换或删除当前会话期间保持旧上下文，并暂时阻止发送。 */
  transitioning: boolean
  /** 列表或详情加载失败时的中文提示（后端不可用时就是这里）。 */
  error: string | null
  refresh: () => Promise<void>
  select: (id: string) => Promise<void>
  /** 只有新会话已成为当前上下文时返回 true，调用方才可清理草稿。 */
  create: () => Promise<boolean>
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
  const [transitioning, setTransitioning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 请求序号：只接受最新一次请求的结果（见文件头竞态说明）。
  // 列表与详情各用一个计数器 —— 共用一个会出事：发完消息触发的列表刷新
  // 会把仍在飞行中的"详情"请求判成过期，导致点开会话后对话区空白。
  const listSeq = useRef(0)
  const detailSeq = useRef(0)
  const activeRef = useRef<string | null>(null)
  const pendingTargetRef = useRef<string | null>(null)

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
    pendingTargetRef.current = id
    setTransitioning(true)
    try {
      const data = await getSession(id)
      if (seq !== detailSeq.current) return
      // 会话标识与详情一起提交，不能让旧对话发往尚未加载的新会话。
      activeRef.current = id
      setActiveId(id)
      setDetail(data)
      setError(null)
    } catch (err) {
      if (seq !== detailSeq.current) return
      // 失败时保留旧上下文；刷新列表以反映被删除或重启后失效的会话。
      await refresh()
      if (seq === detailSeq.current) setError(describe(err))
    } finally {
      if (seq === detailSeq.current) {
        pendingTargetRef.current = null
        setTransitioning(false)
      }
    }
  }, [refresh])

  const create = useCallback(async () => {
    // 发起新建时就抢占旧详情请求；等新建返回后再抢已经太晚。
    const seq = ++detailSeq.current
    pendingTargetRef.current = null
    setTransitioning(true)
    try {
      const created = await createSession()
      if (seq !== detailSeq.current) return false
      activeRef.current = created.id
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
      return seq === detailSeq.current
    } catch (err) {
      if (seq === detailSeq.current) setError(describe(err))
      return false
    } finally {
      if (seq === detailSeq.current) setTransitioning(false)
    }
  }, [refresh])

  const remove = useCallback(
    async (id: string) => {
      // 尚在加载的目标也可能被删除，不能让迟到 GET 把它重新选中。
      const seq = activeRef.current === id || pendingTargetRef.current === id
        ? ++detailSeq.current : null
      if (seq !== null) {
        pendingTargetRef.current = null
        setTransitioning(true)
      }
      try {
        await deleteSession(id)
        // 删除等待期间可以再次点击同一个目标；删除成功使这次加载也失效。
        if (pendingTargetRef.current === id) {
          detailSeq.current += 1
          pendingTargetRef.current = null
          setTransitioning(false)
        }
        if (activeRef.current === id) {
          activeRef.current = null
          setActiveId(null)
          setDetail(null)
        }
        await refresh()
      } catch (err) {
        if (seq === null || seq === detailSeq.current) setError(describe(err))
      } finally {
        if (seq !== null && seq === detailSeq.current) setTransitioning(false)
      }
    },
    [refresh],
  )

  const deselect = useCallback(() => {
    detailSeq.current += 1
    pendingTargetRef.current = null
    activeRef.current = null
    setActiveId(null)
    setDetail(null)
    setTransitioning(false)
    setError(null)
  }, [])

  // 首次挂载拉一次列表
  useEffect(() => {
    void refresh()
    return () => {
      listSeq.current += 1
      detailSeq.current += 1
      pendingTargetRef.current = null
    }
  }, [refresh])

  return {
    sessions,
    backend,
    activeId,
    detail,
    loading,
    transitioning,
    error,
    refresh,
    select,
    create,
    remove,
    deselect,
  }
}
