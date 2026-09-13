/**
 * 应用外壳：把各个 Hook 与组件装在一起。
 *
 * ============================================================
 * 状态放在哪儿，是个需要说清楚的决策
 * ============================================================
 * 这里**没有**引入 Redux / Zustand / Jotai。理由不是"依赖越少越好"这么笼统，
 * 而是这个应用的状态恰好能分成三块，且天然对齐 React 自带的工具：
 *
 *   1. 服务端状态（会话列表、会话详情、元信息）
 *      → 每个都用一个小 Hook 管（useSessions / useServerInfo），
 *        内部处理请求序号、错误、加载态。这部分本来就不该进全局 store。
 *   2. 流式对话状态（消息列表 + 当前轮次的事件流）
 *      → useChat。它的生命周期与"对话区"一致，而且是高频更新
 *        （每个 token 一次），放进全局 store 只会让所有订阅者一起重渲染。
 *   3. 纯 UI 状态（草稿、侧边栏开合、抽屉开合）
 *      → 就地 useState。
 *
 * 真正需要"跨组件共享"的东西其实只有"当前会话 id"，而它已经由 useSessions
 * 提供，通过 props 往下传一层就够了。为这一层引入状态库是负收益。
 *
 * ============================================================
 * 无状态模式不是缺陷，是特性
 * ============================================================
 * 没有选中会话时，请求不携带 session_id，后端就退回"历史由客户端提供"的无状态模式。
 * 这在后端的 routes.py 里是刻意的设计（脚本/CI/一次性的独立提问不需要会话）。
 * 界面不把这个模式藏起来：输入框上方有一条提示说明"本轮不写入会话"，
 * 并给出新建会话的入口 —— 让架构上的两种模式都能被看见。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AGENT_MODE_META } from './lib/types'
import type { AgentMode } from './lib/types'

import { Composer } from './components/Composer'
import { EmptyState } from './components/EmptyState'
import { MessageList } from './components/MessageList'
import { Notice } from './components/Notice'
import { SessionSidebar } from './components/SessionSidebar'
import { ToolsDrawer } from './components/ToolsDrawer'
import { TopBar } from './components/TopBar'
import { useChat } from './hooks/useChat'
import { useServerInfo } from './hooks/useServerInfo'
import { useSessions } from './hooks/useSessions'
import { useStickToBottom } from './hooks/useStickToBottom'
import { useTheme } from './hooks/useTheme'

const MOBILE_BREAKPOINT = 760

function isNarrow(): boolean {
  return typeof window !== 'undefined' && window.innerWidth <= MOBILE_BREAKPOINT
}

export default function App() {
  const { preference, cycle } = useTheme()
  const server = useServerInfo()
  const sessions = useSessions()

  // 一轮结束后刷新列表：标题、轮次数、token 统计都在服务端更新了
  const chat = useChat({ onTurnSettled: sessions.refresh })

  const [draft, setDraft] = useState('')
  const [toolsOpen, setToolsOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(() => !isNarrow())

  /**
   * Agent 形态。
   *
   * 可选列表**来自后端**（`/api/meta` 的 `agent_modes`）而不是前端硬编码：
   * 后端新增一种形态时，前端不改代码就能渲染出对应按钮。
   * 后端还没返回时用 react 兜底 —— 它始终是默认形态。
   */
  const availableModes = useMemo<AgentMode[]>(() => {
    const raw = server.meta?.agent_modes
    if (!Array.isArray(raw) || raw.length === 0) return ['react']
    return raw.filter((m): m is AgentMode => m in AGENT_MODE_META)
  }, [server.meta])

  const [mode, setMode] = useState<AgentMode>('react')

  // 后端声明里没有当前形态时（例如后端降级/换版本），回退到第一个可用形态，
  // 否则用户会停在一个"选了但发出去后端不认识"的状态上
  useEffect(() => {
    if (availableModes.length > 0 && !availableModes.includes(mode)) {
      setMode(availableModes[0] as AgentMode)
    }
  }, [availableModes, mode])

  const scrollRef = useRef<HTMLDivElement | null>(null)
  // 依赖整个 items 数组：流式时它每个 token 都会换新引用，从而触发黏底滚动
  useStickToBottom(scrollRef, chat.items)

  // 载入会话历史：详情变化（切换/新建会话）时重建对话区
  useEffect(() => {
    chat.loadHistory(sessions.detail ? sessions.detail.turns : [])
  }, [sessions.detail, chat.loadHistory])

  // 窄屏时自动收起侧边栏（抽屉形态）
  useEffect(() => {
    const onResize = (): void => {
      if (isNarrow()) setSidebarOpen(false)
    }
    if (isNarrow()) setSidebarOpen(false)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  const handleSelect = useCallback(
    (id: string) => {
      void sessions.select(id)
      setSidebarOpen(!isNarrow())
    },
    [sessions],
  )

  const handleCreate = useCallback(() => {
    void sessions.create()
    setDraft('')
    setSidebarOpen(!isNarrow())
  }, [sessions])

  const handleSend = useCallback(() => {
    const text = draft.trim()
    if (text === '' || chat.isStreaming) return
    setDraft('')
    // 没有会话时传 null：后端会走无状态模式（忽略 history、不落库）
    void chat.send(text, sessions.activeId, mode)
  }, [chat, draft, sessions.activeId, mode])

  const handleRefresh = useCallback(() => {
    void server.reload()
    void sessions.refresh()
  }, [server, sessions])

  const offline = server.offline
  const hasMessages = chat.items.length > 0

  return (
    <div className="app">
      <TopBar
        meta={server.meta}
        health={server.health}
        sessionId={sessions.activeId}
        streaming={chat.isStreaming}
        loading={sessions.loading || server.loading}
        sidebarOpen={sidebarOpen}
        themePreference={preference}
        onToggleSidebar={() => setSidebarOpen((value) => !value)}
        onOpenTools={() => setToolsOpen(true)}
        onRefresh={handleRefresh}
        onCycleTheme={cycle}
      />

      <div className="workspace" data-sidebar={sidebarOpen ? 'open' : 'collapsed'}>
        <SessionSidebar
          sessions={sessions.sessions}
          backend={sessions.backend}
          activeId={sessions.activeId}
          loading={sessions.loading}
          onSelect={handleSelect}
          onCreate={handleCreate}
          onDelete={(id) => {
            void sessions.remove(id)
          }}
          onRefresh={() => {
            void sessions.refresh()
          }}
        />

        {sidebarOpen ? (
          <button
            type="button"
            className="scrim"
            aria-label="收起会话列表"
            onClick={() => setSidebarOpen(false)}
          />
        ) : null}

        <main className="chat">
          <div className="chat__scroll" ref={scrollRef}>
            {offline ? (
              <div className="empty">
                <div className="empty__inner">
                  <Notice
                    tone="error"
                    role="alert"
                    title="无法连接后端服务"
                    text={server.error ?? ''}
                    hint={'.\\scripts\\dev.ps1 serve\n# 就绪后可访问 http://127.0.0.1:8000/docs'}
                    action={
                      <button type="button" className="btn btn--primary" onClick={handleRefresh}>
                        重试连接
                      </button>
                    }
                  />
                  <div className="muted" style={{ fontSize: 'var(--fs-sm)', lineHeight: 1.7 }}>
                    开发期前端通过 vite 代理访问后端：<code className="mono">/api</code> 与{' '}
                    <code className="mono">/healthz</code> 都会被转发到{' '}
                    <code className="mono">http://127.0.0.1:8000</code>。
                    若后端已在运行，请检查 <code className="mono">vite.config.ts</code> 里的代理目标
                    （可用环境变量 <code className="mono">JOBPILOT_BACKEND</code> 覆盖）。
                  </div>
                </div>
              </div>
            ) : (
              <div className="chat__inner">
                {hasMessages ? (
                  <MessageList items={chat.items} />
                ) : (
                  <EmptyState meta={server.meta} health={server.health} onPick={setDraft} />
                )}
                {chat.notice ? <Notice tone="info" title={chat.notice} /> : null}
              </div>
            )}
          </div>

          <div className="composer-wrap">
            <div className="composer-wrap__inner">
              {sessions.error && !offline ? (
                <Notice
                  tone="warn"
                  title="会话操作失败"
                  text={sessions.error}
                  action={
                    <button
                      type="button"
                      className="btn"
                      onClick={() => {
                        void sessions.refresh()
                      }}
                    >
                      刷新列表
                    </button>
                  }
                />
              ) : null}

              {server.error && !offline ? (
                <Notice
                  tone="warn"
                  title="部分接口不可用"
                  text={`${server.error}（对话仍可正常使用）`}
                  action={
                    <button type="button" className="btn" onClick={handleRefresh}>
                      重试
                    </button>
                  }
                />
              ) : null}

              <Composer
                value={draft}
                onChange={setDraft}
                onSend={handleSend}
                onStop={chat.abort}
                streaming={chat.isStreaming}
                disabled={offline}
                modes={availableModes}
                current={mode}
                onModeChange={setMode}
              />

              {!offline && sessions.activeId === null ? (
                <div className="mode-hint">
                  <span className="dot dot--warn" />
                  <span>
                    无状态模式：本轮不会写入任何会话（后端在不传 session_id 时的既有行为）
                  </span>
                  <span className="mode-hint__spacer" />
                  <button type="button" className="btn" onClick={handleCreate}>
                    新建会话以获得多轮记忆
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </main>
      </div>

      <ToolsDrawer
        open={toolsOpen}
        tools={server.tools}
        meta={server.meta}
        onClose={() => setToolsOpen(false)}
      />
    </div>
  )
}
