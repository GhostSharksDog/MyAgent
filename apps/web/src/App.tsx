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
import { getAccessKey, resolveAccessNotice } from './lib/access'
import { startFolderPick } from './lib/picker'
import { fetchPickerCapability, pickDirectory } from './lib/settings-api'
import { AGENT_MODE_META } from './lib/types'
import type { AgentMode } from './lib/types'

import { Composer } from './components/Composer'
import { EmptyState } from './components/EmptyState'
import { MessageList } from './components/MessageList'
import { Notice } from './components/Notice'
import { SessionSidebar } from './components/SessionSidebar'
import { FileSidebar } from './components/FileSidebar'
import { FolderPicker } from './components/FolderPicker'
import { SettingsPanel } from './components/SettingsPanel'
import { ToolsDrawer } from './components/ToolsDrawer'
import { TopBar } from './components/TopBar'
import { useChat } from './hooks/useChat'
import { useServerInfo } from './hooks/useServerInfo'
import { useSessions } from './hooks/useSessions'
import { useSettings } from './hooks/useSettings'
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
  // 设置面板：模型接入 / 身份 / 知识库 / 工作区。
  // 文件浏览器要读它的 workspaceRoot 与 configured，
  // 所以提到同一个层级，而不是各自请求一遍。
  const settings = useSettings()

  // 一轮结束后刷新列表：标题、轮次数、token 统计都在服务端更新了
  const chat = useChat({ onTurnSettled: sessions.refresh })

  const [draft, setDraft] = useState('')
  const [toolsOpen, setToolsOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  // 右侧文件栏是否展开。默认收起：不是每次对话都需要看文件，
  // 但它是一条**常驻的一等区域**，展开后与对话并排，不遮挡。
  const [fileSidebarOpen, setFileSidebarOpen] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  // 系统对话框正开着（等用户操作）。它会一直挂着，所以按钮必须显示"在等"，
  // 否则用户会以为没反应而再点一次 —— 而第二次会被服务端拒绝（只允许一个对话框）。
  const [folderPicking, setFolderPicking] = useState(false)
  // 退回面板时要带过去的原因（为什么没能直接用系统对话框）
  const [pickerNote, setPickerNote] = useState('')

  // 首次打开任一面板时拉一次设置 ——
  // 文件浏览需要知道"工作区配了没有"，设置面板需要当前值。
  // 放在 App 而不是各自组件里，是为了两个面板共享同一份数据：
  // 用户在设置里改完工作区，文件浏览器立刻能用，不用重开。
  useEffect(() => {
    if (settingsOpen) void settings.load()
    // settings.load 是 useCallback 稳定的；不把 settings 整个放进来，
    // 否则每次渲染都会因为它返回新对象而重新触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsOpen, settings.load])
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

  // 文件功能是否可用。**两处用的是同一个判定**（顶栏按钮与文件面板）：
  // 分开写迟早会漂移，而漂移的表现是"按钮能点但面板说未配置"这种自相矛盾。
  const workspaceConfigured =
    settings.saved != null && settings.saved.agent.workspace_root.trim() !== ''

  /**
   * 选中一个文件夹作为工作区。
   *
   * 【为什么选完自动展开文件栏】
   * 用户选文件夹的目的就是"要看它"。选完还停在收起状态，等于要他再点一次 ——
   * 而那个多余的动作会让人怀疑"我选上了吗"。
   * **动作的结果应当立刻可见。**
   */
  const handlePickFolder = useCallback(
    async (absolutePath: string) => {
      setPickerOpen(false)
      setPickerNote('')
      const ok = await settings.save({ workspace_root: absolutePath })
      if (ok) setFileSidebarOpen(true)
    },
    [settings],
  )

  const handleCloseFolder = useCallback(async () => {
    await settings.save({ workspace_root: '' })
    setFileSidebarOpen(false)
  }, [settings])

  /**
   * 点"打开文件夹"：**直接弹系统对话框**，不先开面板。
   *
   * 【为什么是"直接"，而不是"打开一个选择面板"】
   * 用户点这个按钮的意图在能用系统对话框时一步就能满足。中间那一屏
   * 只是把同一个动作拆成两次点击，还要求用户先理解"系统对话框"是什么。
   *
   * 面板只在两种情况下出现：宿主弹不出对话框（远程部署），或者弹的过程中
   * 出错 —— 那时它是唯一可行的兜底，顺带把原因显示出来。
   *
   * 【为什么要有 folderPicking】
   * 请求会一直挂着直到用户点完（可能几分钟），而对话框弹出来需要一点点时间。
   * 这期间按钮必须是"正在等待"的样子：否则用户会以为没反应而再点一次，
   * 而第二次会被服务端拒绝（同一时刻只允许一个对话框）——
   * 那时他看到的是一个让人困惑的错误。
   */
  const handleOpenFolder = useCallback(async () => {
    setFolderPicking(true)
    try {
      const result = await startFolderPick({ fetchPickerCapability, pickDirectory })
      if (result.kind === 'picked') {
        await handlePickFolder(result.path)
        return
      }
      if (result.kind === 'cancelled') return  // 取消：什么都不做，也不提示

      setPickerNote(result.kind === 'error' ? result.message : result.reason)
      setPickerOpen(true)
    } finally {
      setFolderPicking(false)
    }
  }, [handlePickFolder])

  const offline = server.offline
  const hasMessages = chat.items.length > 0

  // 服务端要不要密钥 × 本地有没有密钥 → 界面该不该提示（三态，见 lib/access.ts）。
  // 用 healthz 里的 auth_required 而不是"撞到 401 再说"：撞到时用户看到的
  // 只是"请求失败"，而该做的是去设置里填密钥。
  const accessNotice = resolveAccessNotice(
    server.health?.auth_required === true,
    getAccessKey() !== '',
  )

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
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenFiles={() => setFileSidebarOpen((v) => !v)}
        filesOpen={fileSidebarOpen}
        filesAvailable={workspaceConfigured}
        onRefresh={handleRefresh}
        onCycleTheme={cycle}
      />

      <div
        className="workspace"
        data-sidebar={sidebarOpen ? 'open' : 'collapsed'}
        data-files={fileSidebarOpen ? 'open' : 'collapsed'}
      >
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
          workspaceRoot={settings.saved?.agent.workspace_root ?? ''}
          fileSidebarOpen={fileSidebarOpen}
          folderPicking={folderPicking}
          // 直接弹系统对话框 —— 面板只在弹不出时作为兜底出现
          onOpenFolder={() => void handleOpenFolder()}
          onCloseFolder={() => void handleCloseFolder()}
          onToggleFileSidebar={() => setFileSidebarOpen((v) => !v)}
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
                    （可用环境变量 <code className="mono">LEGACY_BACKEND</code> 覆盖）。
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

              {/*
                服务端启用了访问密钥、而本地还没填 —— 这是**唯一**能让用户
                看懂"为什么什么都失败"的提示。

                没有它的话，用户看到的是每个接口各报一条 401，
                而真正该做的事是"去设置里填密钥"，那是完全不同的方向。
                所以这里给的是**动作**，不是一句"出错了"。
              */}
              {accessNotice === 'required' && !offline ? (
                <Notice
                  tone="warn"
                  title="这台服务要求访问密钥"
                  text="后端启用了 SECURITY_API_KEY，因此 /api 请求都需要带上密钥。填入后立即生效，不需要重启服务。"
                  action={
                    <button
                      type="button"
                      className="btn btn--primary"
                      onClick={() => setSettingsOpen(true)}
                    >
                      去填写密钥
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

        {/* 文件栏必须**在 .workspace 网格里面** ——
            它是网格的第三列。
            我第一版把它渲染在 .workspace 之外（因为原来的抽屉就是放在那里的），
            结果它成了一个普通块级元素，被堆在对话区**下方**，
            而不是排在右侧。三列的 CSS 在，但那一列从来没有人站进去。

            **网格子项的身份由"在不在这个容器里"决定，不由 CSS 决定。**
            这类错误 typecheck 和构建都不会报 —— 只是看起来不对。 */}
        {fileSidebarOpen && (
          <FileSidebar
            root={settings.saved?.agent.workspace_root ?? ''}
            onClose={() => setFileSidebarOpen(false)}
            onOpenFolder={() => setPickerOpen(true)}
            onSwitchFolder={() => setPickerOpen(true)}
          />
        )}
      </div>

      <ToolsDrawer
        open={toolsOpen}
        tools={server.tools}
        meta={server.meta}
        onClose={() => setToolsOpen(false)}
      />

      <SettingsPanel
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        settings={settings}
      />

      <FolderPicker
        open={pickerOpen}
        current={settings.saved?.agent.workspace_root}
        note={pickerNote}
        onClose={() => {
          setPickerOpen(false)
          setPickerNote('')
        }}
        onPick={(p) => void handlePickFolder(p)}
      />
    </div>
  )
}
