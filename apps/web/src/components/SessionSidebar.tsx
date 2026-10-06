import { useRef, useState } from 'react'
import { useDialogFocus } from '../hooks/useDialogFocus'
import { formatRelativeTime } from '../lib/format'
import type { SessionSummary } from '../lib/types'
import { IconChevronRight, IconFolder, IconPlus, IconRefresh, IconTrash, IconX } from './Icons'

export interface SessionSidebarProps {
  sessions: SessionSummary[]
  backend: string
  activeId: string | null
  loading: boolean
  open: boolean
  modal: boolean
  onClose: () => void
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (id: string) => void
  onRefresh: () => void
  onOpenTools: () => void
  onOpenSettings: () => void
  status: 'loading' | 'offline' | 'unconfigured' | 'ready'
  workspaceRoot: string
  workspaceLoaded: boolean
  workspaceLoading: boolean
  workspaceError: string | null
  workspaceWritable: boolean
  fileSidebarOpen: boolean
  folderPicking: boolean
  onOpenFolder: () => void
  onCloseFolder: () => void
  onToggleFileSidebar: () => void
}

export function SessionSidebar(props: SessionSidebarProps) {
  const {
    sessions, backend, activeId, loading, open, modal, onClose, onSelect, onCreate, onDelete,
    onRefresh, onOpenTools, onOpenSettings, status, workspaceRoot, workspaceLoaded,
    workspaceLoading, workspaceError, workspaceWritable, fileSidebarOpen,
    folderPicking, onOpenFolder, onCloseFolder, onToggleFileSidebar,
  } = props
  const containerRef = useRef<HTMLElement | null>(null)
  useDialogFocus({ open: open && modal, containerRef, onClose })
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const hasWorkspace = workspaceRoot.trim() !== ''
  const labels = { loading: '连接中…', offline: '服务未连接', unconfigured: '待配置模型', ready: '服务已就绪' }

  return (
    <aside id="session-sidebar" ref={containerRef} className="sidebar"
      aria-label="会话列表" aria-hidden={!open} inert={!open}
      role={modal ? 'dialog' : undefined} aria-modal={modal && open ? true : undefined}>
      <div className="sidebar__head">
        <button type="button" className="btn sidebar__new" onClick={onCreate} disabled={loading}
          aria-label="新建对话">
          <IconPlus size={17} />新建对话
        </button>
        <button type="button" className="btn btn--ghost btn--icon sidebar__close"
          onClick={onClose} aria-label="收起会话列表"><IconX size={17} /></button>
      </div>
      <div className="sidebar__caption">最近的对话</div>
      <div className="sidebar__list">
        {sessions.length === 0 ? (
          <div className="sidebar__empty">
            {loading ? '正在读取对话…' : <><p>还没有对话</p><span>新建一个，留住你的思路。</span></>}
          </div>
        ) : sessions.map((session) => (
          <div className="session-item" key={session.id} data-active={session.id === activeId}>
            <button type="button" className="session-item__select"
              onClick={() => onSelect(session.id)} aria-current={session.id === activeId}
              title={session.title || '新对话'}>
              <span className="session-item__title">{session.title || '新对话'}</span>
              <span className="session-item__meta">{formatRelativeTime(session.updated_at)}</span>
            </button>
            <button type="button" className="session-item__delete" aria-label="删除会话"
              data-confirm={pendingDelete === session.id}
              title={pendingDelete === session.id ? '再点一次确认删除' : '删除对话'}
              onClick={() => {
                if (pendingDelete === session.id) {
                  setPendingDelete(null)
                  onDelete(session.id)
                } else setPendingDelete(session.id)
              }}
              onBlur={() => setPendingDelete(null)}>
              <IconTrash size={14} />
            </button>
          </div>
        ))}
      </div>
      <div className="sidebar__foot">
        <div className="sidebar__workspace">
          <span className="sidebar__caption">工作区</span>
          {workspaceLoaded && workspaceError && <div className="ws__pending" role="status">
            <span>配置更新未完成</span><button type="button" className="link-button"
              title={workspaceError} onClick={onOpenSettings}>查看原因</button>
          </div>}
          {!workspaceLoaded ? (
            <div className="ws__pending">
              <span>{workspaceLoading ? '正在读取工作区…' : workspaceError ? '工作区信息暂不可用' : '尚未读取工作区'}</span>
              {!workspaceLoading && <button type="button" className="link-button"
                onClick={onOpenSettings}>{workspaceError ? '查看原因与设置' : '打开设置'}</button>}
            </div>
          ) : hasWorkspace ? (
            <div className="ws">
              <button type="button" className="ws__browse" onClick={onToggleFileSidebar}
                aria-label="工作区文件" aria-expanded={fileSidebarOpen} title={workspaceRoot}>
                <IconFolder size={17} />
                <span className="ws__body">
                  <span className="ws__name">{workspaceRoot.split(/[\\/]/).filter(Boolean).pop()}</span>
                  <span className="ws__permission" data-writable={workspaceWritable}>{workspaceWritable ? '允许读写' : '只读访问'}</span>
                </span>
                <IconChevronRight size={13} />
              </button>
              <details className="ws__menu">
                <summary>管理工作区</summary>
                <span className="ws__full" title={workspaceRoot}>{workspaceRoot}</span>
                <div className="ws__actions">
                  <button type="button" className="btn btn--ghost" onClick={onOpenFolder}
                    disabled={folderPicking}>{folderPicking ? '等待选择…' : '更换目录'}</button>
                  <button type="button" className="btn btn--danger" onClick={onCloseFolder}>移除工作区</button>
                </div>
              </details>
            </div>
          ) : (
            <button type="button" className="ws__open" onClick={onOpenFolder}
              disabled={folderPicking} aria-label="选择工作区">
              <IconFolder size={17} /><span>{folderPicking ? '等待选择…' : '选择工作区'}</span><IconPlus size={14} />
            </button>
          )}
        </div>
        <div className="sidebar__service">
          <button type="button" className="sidebar__status" onClick={onOpenTools}
            aria-label="查看服务状态">
            <span className={status === 'ready' ? 'dot dot--ok' : status === 'loading' ? 'dot dot--live' : 'dot dot--warn'} />
            {labels[status]}
          </button>
          <button type="button" className="btn btn--ghost btn--icon" onClick={onRefresh}
            aria-label="刷新服务状态" title="刷新服务与对话" disabled={loading}>
            <IconRefresh size={14} className={loading ? 'spin' : undefined} />
          </button>
        </div>
        {backend === 'memory' ? <p className="sidebar__storage">重启后历史不会保留</p>
          : backend ? <p className="sidebar__storage">历史存储：{backend === 'sql' || backend === 'sqlite' ? '本地数据库' : backend === 'postgresql' ? 'PostgreSQL' : backend}</p> : null}
      </div>
    </aside>
  )
}
