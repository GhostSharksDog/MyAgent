import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { getAccessKey, resolveAccessNotice } from './lib/access'
import { startFolderPick } from './lib/picker'
import { fetchPickerCapability, pickDirectory } from './lib/settings-api'
import { AGENT_MODE_META } from './lib/types'
import type { AgentMode } from './lib/types'
import { Composer } from './components/Composer'
import { EmptyState, StarterPrompts } from './components/EmptyState'
import { MessageList } from './components/MessageList'
import { Notice } from './components/Notice'
import { SessionSidebar } from './components/SessionSidebar'
import { FileSidebar } from './components/FileSidebar'
import { FolderPicker } from './components/FolderPicker'
import { SettingsDialog } from './components/SettingsDialog'
import type { SettingsSection } from './components/SettingsDialog'
import { ToolsDrawer } from './components/ToolsDrawer'
import { TopBar } from './components/TopBar'
import { useChat } from './hooks/useChat'
import { useServerInfo } from './hooks/useServerInfo'
import { useSessions } from './hooks/useSessions'
import { useSettings } from './hooks/useSettings'
import { useStickToBottom } from './hooks/useStickToBottom'
import { usePreferences } from './hooks/usePreferences'
import { useTheme } from './hooks/useTheme'

const MOBILE_BREAKPOINT = 760
const FILE_OVERLAY_BREAKPOINT = 1200

export default function App() {
  const theme = useTheme()
  const prefs = usePreferences()
  const server = useServerInfo()
  const sessions = useSessions()
  const settings = useSettings()
  const chat = useChat({ onTurnSettled: sessions.refresh })
  const [width, setWidth] = useState(window.innerWidth)
  const widthRef = useRef(width)
  const narrow = width <= MOBILE_BREAKPOINT
  const fileOverlay = width < FILE_OVERLAY_BREAKPOINT
  const [draft, setDraft] = useState('')
  const [toolsOpen, setToolsOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSection, setSettingsSection] = useState<SettingsSection>('general')
  const [fileSidebarOpen, setFileSidebarOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > MOBILE_BREAKPOINT)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [folderPicking, setFolderPicking] = useState(false)
  const [pickerNote, setPickerNote] = useState('')
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const sidebarToggleRef = useRef<HTMLButtonElement | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const onResize = () => {
      const next = window.innerWidth
      if (next <= MOBILE_BREAKPOINT && widthRef.current > MOBILE_BREAKPOINT) {
        setSidebarOpen(false)
      }
      widthRef.current = next
      setWidth(next)
    }
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  // Read the declared workspace once the service is available, rather than
  // treating settings that have not loaded as an empty workspace.
  useEffect(() => {
    if (server.health && !server.offline) void settings.load()
  }, [server.health?.status, server.health?.auth_required, server.offline, settings.load])

  const availableModes = useMemo<AgentMode[]>(() => {
    const raw = server.meta?.agent_modes
    if (!Array.isArray(raw) || raw.length === 0) return ['react']
    const known = raw.filter((m): m is AgentMode => m in AGENT_MODE_META)
    return known.length ? known : ['react']
  }, [server.meta])
  const [mode, setMode] = useState<AgentMode>('react')
  useEffect(() => {
    if (!availableModes.includes(mode)) setMode(availableModes[0] ?? 'react')
  }, [availableModes, mode])

  useStickToBottom(scrollRef, chat.items, prefs.prefs.autoScroll)
  useEffect(() => {
    chat.loadHistory(sessions.detail?.turns ?? [])
  }, [sessions.detail, chat.loadHistory])

  const openSettings = useCallback((section: SettingsSection = 'general') => {
    setSettingsSection(section)
    setSettingsOpen(true)
  }, [])
  const handleRefresh = useCallback(() => {
    void server.reload()
    void sessions.refresh()
    void settings.load()
  }, [server.reload, sessions.refresh, settings.load])
  const handleConfigurationUpdated = useCallback(() => {
    void server.reload()
    void settings.load()
  }, [server.reload, settings.load])

  const handleCreate = useCallback(() => {
    void sessions.create()
    if (narrow) setSidebarOpen(false)
    inputRef.current?.focus()
  }, [sessions.create, narrow])
  const handleSelect = useCallback((id: string) => {
    void sessions.select(id)
    if (narrow) setSidebarOpen(false)
  }, [sessions.select, narrow])
  const handleSend = useCallback(() => {
    const text = draft.trim()
    if (!text || chat.isStreaming || sessions.transitioning) return
    setDraft('')
    void chat.send(text, sessions.activeId, mode)
  }, [draft, chat.send, chat.isStreaming, sessions.activeId, sessions.transitioning, mode])
  const handleStarter = useCallback((text: string) => {
    setDraft(text)
    inputRef.current?.focus()
  }, [])
  const toggleSidebar = useCallback(() => {
    if (!sidebarOpen && narrow) setFileSidebarOpen(false)
    setSidebarOpen((value) => !value)
  }, [sidebarOpen, narrow])
  const toggleFiles = useCallback(() => {
    if (!fileSidebarOpen && narrow) setSidebarOpen(false)
    setFileSidebarOpen((value) => !value)
  }, [fileSidebarOpen, narrow])
  const closeFiles = useCallback(() => {
    setFileSidebarOpen(false)
    // 手机上原入口随会话抽屉一起隐藏，归还到始终可见的会话入口。
    if (narrow) requestAnimationFrame(() => sidebarToggleRef.current?.focus())
  }, [narrow])

  const handlePickFolder = useCallback(async (absolutePath: string) => {
    setPickerOpen(false)
    setPickerNote('')
    if (await settings.save({ workspace_root: absolutePath })) {
      setFileSidebarOpen(true)
      if (widthRef.current <= MOBILE_BREAKPOINT) setSidebarOpen(false)
      void server.reload()
    }
  }, [settings.save, server.reload])
  const handleCloseFolder = useCallback(async () => {
    if (await settings.save({ workspace_root: '' })) {
      setFileSidebarOpen(false)
      void server.reload()
    }
  }, [settings.save, server.reload])
  const handleOpenFolder = useCallback(async () => {
    setFolderPicking(true)
    try {
      const result = await startFolderPick({ fetchPickerCapability, pickDirectory })
      if (result.kind === 'picked') {
        await handlePickFolder(result.path)
      } else if (result.kind !== 'cancelled') {
        setPickerNote(result.kind === 'error' ? result.message : result.reason)
        setPickerOpen(true)
      }
    } finally { setFolderPicking(false) }
  }, [handlePickFolder])

  const offline = server.offline
  const hasMessages = chat.items.length > 0
  const welcome = !offline && !hasMessages
  const accessNotice = resolveAccessNotice(server.health?.auth_required === true, getAccessKey() !== '')
  const status = offline ? 'offline' : !server.health ? 'loading'
    : server.health.llm_configured ? 'ready' : 'unconfigured'

  return (
    <div className="app">
      <TopBar meta={server.meta} health={server.health} streaming={chat.isStreaming}
        sidebarToggleRef={sidebarToggleRef}
        sidebarOpen={sidebarOpen} offline={offline} onToggleSidebar={toggleSidebar}
        onOpenModels={() => openSettings('models')} onOpenTools={() => setToolsOpen(true)}
        onOpenSettings={() => openSettings()} />
      <div className="workspace" data-sidebar={sidebarOpen ? 'open' : 'collapsed'}
        data-files={fileSidebarOpen ? 'open' : 'collapsed'}>
        <SessionSidebar sessions={sessions.sessions} backend={sessions.backend}
          activeId={sessions.activeId} loading={sessions.loading} open={sidebarOpen} modal={narrow}
          onClose={() => setSidebarOpen(false)} onSelect={handleSelect} onCreate={handleCreate}
          onDelete={(id) => void sessions.remove(id)} onRefresh={handleRefresh}
          onOpenTools={() => setToolsOpen(true)} onOpenSettings={() => openSettings('workspace')}
          status={status} workspaceRoot={settings.saved?.agent.workspace_root ?? ''}
          workspaceLoaded={settings.saved !== null} workspaceLoading={settings.loading}
          workspaceError={settings.error} workspaceWritable={settings.saved?.agent.file_write_enabled === true}
          fileSidebarOpen={fileSidebarOpen} folderPicking={folderPicking}
          onOpenFolder={() => void handleOpenFolder()} onCloseFolder={() => void handleCloseFolder()}
          onToggleFileSidebar={toggleFiles} />
        {sidebarOpen && narrow && <button type="button" className="scrim" tabIndex={-1}
          aria-label="收起会话列表" onClick={() => setSidebarOpen(false)} />}
        <main className={welcome ? 'chat chat--welcome' : 'chat'} data-empty={welcome}>
          <div className="chat__scroll" ref={scrollRef}>
            {offline ? (
              <div className="chat__inner chat__offline">
                <Notice tone="error" role="alert" title="暂时无法连接服务"
                  text={server.error ?? '请先启动本地服务，然后重试。'}
                  action={<button type="button" className="btn" onClick={handleRefresh}>重试连接</button>} />
              </div>
            ) : hasMessages ? (
              <div className="chat__inner"><MessageList items={chat.items} showMeta={prefs.prefs.showMeta} /></div>
            ) : <EmptyState />}
          </div>
          <div className="composer-wrap">
            <div className="composer-wrap__inner">
              {sessions.error && !offline && <Notice tone="warn" title="对话列表暂不可用"
                text={sessions.error} action={<button type="button" className="btn"
                  onClick={() => void sessions.refresh()}>重试</button>} />}
              {server.error && !offline && <Notice tone="warn" title="部分信息暂不可用"
                text={server.error} action={<button type="button" className="btn" onClick={handleRefresh}>重试</button>} />}
              {accessNotice === 'required' && !offline ? (
                <Notice tone="warn" title="填写访问密钥后连接" text="这台服务已启用访问控制。"
                  action={<button type="button" className="btn" onClick={() => openSettings('access')}>填写访问密钥</button>} />
              ) : server.health?.llm_configured === false && !offline ? (
                <Notice tone="info" title="连接一个模型，开始对话"
                  action={<button type="button" className="btn" onClick={() => openSettings('models')}>配置模型</button>} />
              ) : null}
              <Composer value={draft} onChange={setDraft} onSend={handleSend} onStop={chat.abort}
                streaming={chat.isStreaming} disabled={offline || sessions.transitioning} modes={availableModes}
                current={mode} onModeChange={setMode} sendWith={prefs.prefs.sendWith} inputRef={inputRef} />
              <div className="composer-context">
                <span>{sessions.transitioning ? '正在打开对话…' : sessions.activeId === null ? '不保存此次对话' : mode === 'react' ? '可使用会话历史' : '独立任务'}</span>
                {sessions.activeId === null && <button type="button" className="link-button"
                  onClick={handleCreate}>新建对话</button>}
                {chat.notice && <span role="status" className="composer-context__notice">{chat.notice}</span>}
              </div>
              {welcome && <StarterPrompts onPick={handleStarter} />}
            </div>
          </div>
        </main>
        {fileSidebarOpen && <>
          {fileOverlay && <button type="button" className="files-scrim" tabIndex={-1}
            aria-label="关闭文件面板" onClick={closeFiles} />}
          <FileSidebar root={settings.saved?.agent.workspace_root ?? ''} modal={fileOverlay}
            onClose={closeFiles} onOpenFolder={() => void handleOpenFolder()}
            onSwitchFolder={() => void handleOpenFolder()} />
        </>}
      </div>
      <ToolsDrawer open={toolsOpen} tools={server.tools} meta={server.meta} health={server.health}
        sessionId={sessions.activeId} onRefresh={handleRefresh} onClose={() => setToolsOpen(false)} />
      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)}
        initialSection={settingsSection} onUpdated={handleConfigurationUpdated}
        settings={settings} themePreference={theme.preference} onThemeChange={theme.setPreference}
        prefs={prefs.prefs} onPrefChange={prefs.set} onResetPrefs={prefs.reset} />
      <FolderPicker open={pickerOpen} current={settings.saved?.agent.workspace_root} note={pickerNote}
        onClose={() => { setPickerOpen(false); setPickerNote('') }}
        onPick={(path) => void handlePickFolder(path)} />
    </div>
  )
}
