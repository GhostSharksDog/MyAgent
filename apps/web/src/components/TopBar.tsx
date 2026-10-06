import type { RefObject } from 'react'
import type { ApiMeta, HealthStatus } from '../lib/types'
import { IconChevronRight, IconGear, IconGrid, IconPanelLeft } from './Icons'

export interface TopBarProps {
  meta: ApiMeta | null
  health: HealthStatus | null
  streaming: boolean
  sidebarOpen: boolean
  offline: boolean
  onToggleSidebar: () => void
  onOpenModels: () => void
  onOpenTools: () => void
  onOpenSettings: () => void
  sidebarToggleRef?: RefObject<HTMLButtonElement | null>
}

export function TopBar({
  meta, health, streaming, sidebarOpen, offline,
  onToggleSidebar, onOpenModels, onOpenTools, onOpenSettings, sidebarToggleRef,
}: TopBarProps) {
  const model = meta?.model ?? health?.model ?? '选择模型'
  const ready = health?.llm_configured === true && !offline
  return (
    <header className="topbar">
      <div className="topbar__brand">
        <button type="button" ref={sidebarToggleRef} className="btn btn--ghost btn--icon"
          onClick={onToggleSidebar} aria-label="切换会话列表"
          aria-expanded={sidebarOpen} aria-controls="session-sidebar"
          title={sidebarOpen ? '收起会话列表' : '展开会话列表'}>
          <IconPanelLeft size={18} />
        </button>
        <span className="topbar__mark" aria-hidden="true">L</span>
        <span className="topbar__title">Legacy</span>
      </div>
      <div className="topbar__spacer" />
      <button type="button" className="topbar__model" onClick={onOpenModels}
        aria-label="模型设置" title={model + ' · 打开模型设置'}>
        <span className={streaming ? 'dot dot--live' : ready ? 'dot dot--ok' : 'dot dot--warn'} />
        <span className="topbar__model-name">{model}</span>
        <IconChevronRight size={12} />
      </button>
      <div className="topbar__actions">
        <button type="button" className="btn btn--ghost" onClick={onOpenTools}
          aria-label="查看工具" title="查看可用工具与服务状态">
          <IconGrid size={17} /><span className="topbar__action-label">工具</span>
        </button>
        <button type="button" className="btn btn--ghost" onClick={onOpenSettings}
          aria-label="设置" title="打开设置">
          <IconGear size={17} /><span className="topbar__action-label">设置</span>
        </button>
      </div>
    </header>
  )
}
