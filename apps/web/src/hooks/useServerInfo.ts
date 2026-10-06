/**
 * 服务端元信息 Hook：`/healthz` + `/api/meta` + `/api/tools`。
 *
 * 这三个接口的作用是**让前端的假设变得可验证**：
 * UI 上显示的模型名、工具数量、会话后端，都不是写死的字符串，
 * 而是从后端读回来的。这样"界面显示 deepseek-chat"这件事本身就证明了
 * 前后端确实连上了 —— 对一个演示型作品来说，这比一句"已连接"更有说服力。
 *
 * 用 `Promise.allSettled` 而不是 `Promise.all`：三个接口彼此独立，
 * 任何一个失败都不该让另外两个的信息一起消失（何况 `/api/tools` 挂了
 * 也不影响用户聊天）。部分可用 > 全部不可用。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, fetchHealth, fetchMeta, fetchTools } from '../lib/api'
import type { ApiMeta, HealthStatus, ToolInfo } from '../lib/types'

export interface UseServerInfoResult {
  health: HealthStatus | null
  meta: ApiMeta | null
  tools: ToolInfo[]
  /**
   * 后端整体不可用（`/healthz` 失败）。
   *
   * 刻意与 `error` 分开：如果只有 `/api/tools` 挂了，界面不该整块变成
   * "服务不可用" —— 用户仍然可以正常聊天，只是工具面板是空的。
   * 把"部分失败"当成"整体失败"，是错误处理里最常见的一种过度反应。
   */
  offline: boolean
  /** 需要提示给用户的错误信息（可能是部分失败）。 */
  error: string | null
  loading: boolean
  reload: () => Promise<void>
}

function describe(reason: unknown): string {
  if (reason instanceof ApiError) return reason.detail
  if (reason instanceof Error) return reason.message
  return String(reason)
}

export function useServerInfo(): UseServerInfoResult {
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [meta, setMeta] = useState<ApiMeta | null>(null)
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [error, setError] = useState<string | null>(null)
  const [offline, setOffline] = useState(false)
  const [loading, setLoading] = useState(true)
  const requestSequence = useRef(0)
  const mounted = useRef(true)

  const reload = useCallback(async () => {
    if (!mounted.current) return
    const current = ++requestSequence.current
    setLoading(true)
    const [healthResult, metaResult, toolsResult] = await Promise.allSettled([
      fetchHealth(),
      fetchMeta(),
      fetchTools(),
    ])
    // 配置保存会触发刷新，先前的请求不能把旧模型与工具清单写回来。
    if (!mounted.current || current !== requestSequence.current) return

    if (healthResult.status === 'fulfilled') setHealth(healthResult.value)
    if (metaResult.status === 'fulfilled') setMeta(metaResult.value)
    if (toolsResult.status === 'fulfilled') setTools(toolsResult.value)

    // 健康检查是最轻、也最先该成功的那个：它失败就认定"后端不可用"。
    if (healthResult.status === 'rejected') {
      setOffline(true)
      setError(describe(healthResult.reason))
    } else {
      setOffline(false)
      setError(
        // 只有非致命的一部分失败时给一条警告，而不是把界面判死
        toolsResult.status === 'rejected'
          ? `工具列表加载失败：${describe(toolsResult.reason)}`
          : metaResult.status === 'rejected'
            ? `服务元信息加载失败：${describe(metaResult.reason)}`
            : null,
      )
    }

    setLoading(false)
  }, [])

  useEffect(() => {
    mounted.current = true
    void reload()
    return () => {
      mounted.current = false
      requestSequence.current += 1
    }
  }, [reload])

  return { health, meta, tools, offline, error, loading, reload }
}
