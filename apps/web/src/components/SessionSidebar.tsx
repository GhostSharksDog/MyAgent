/**
 * 左侧会话列表。
 *
 * 后端契约里有两点会直接影响这里的呈现，值得写下来：
 *
 * 1. **列表项不含对话内容**（`SessionSummary` 只有元信息）。
 *    所以这里只显示标题、轮次数、token 和更新时间 —— 不是偷懒，
 *    而是顺着后端"列表轻、详情重"的设计走。
 *
 * 2. **新建的会话标题一开始是空的**。后端只有在**第一轮对话成功落库**之后
 *    才会用首句用户输入生成标题（`Session.append_turn` 里做的）。
 *    因此列表里会先看到"（未命名会话）"，发完第一句才变成有意义的标题。
 *    界面不掩盖这件事：未命名就用灰色斜体显示，让它是"还没开始"的状态。
 *
 * 删除做了二次确认：会话是不可恢复的（内存/Redis 都直接删），
 * 而且在作品集演示时误删当场丢失上下文非常尴尬。
 */

import { useState } from 'react'

import { formatCompact, formatRelativeTime } from '../lib/format'
import type { SessionSummary } from '../lib/types'
import { IconFolder, IconPlus, IconRefresh, IconTrash } from './Icons'

export interface SessionSidebarProps {
  sessions: SessionSummary[]
  backend: string
  activeId: string | null
  loading: boolean
  onSelect: (id: string) => void
  onCreate: () => void
  onDelete: (id: string) => void
  onRefresh: () => void

  // ---------- 工作区（P6 布局改造） ----------
  // 【为什么工作区入口放在会话栏，而不是顶栏或设置里】
  // "打开文件夹"是一个**建立上下文**的动作，和"新建会话"是同一类事情：
  // 它们都决定了接下来这场对话在什么范围内发生。
  // 放在一起，用户一眼就能看懂"左边这块是管上下文的"。
  //
  // 而文件浏览是**使用**这个上下文的地方，所以它在右侧边栏常驻 ——
  // 一处建立、一处使用，各归其位。
  workspaceRoot: string
  fileSidebarOpen: boolean
  /** 系统对话框正开着（等用户操作）。期间按钮要显示"在等"，不能再触发第二次。 */
  folderPicking: boolean
  onOpenFolder: () => void
  onCloseFolder: () => void
  onToggleFileSidebar: () => void
}

export function SessionSidebar({
  sessions,
  backend,
  activeId,
  loading,
  onSelect,
  onCreate,
  onDelete,
  onRefresh,
  workspaceRoot,
  fileSidebarOpen,
  folderPicking,
  onOpenFolder,
  onCloseFolder,
  onToggleFileSidebar,
}: SessionSidebarProps) {
  const hasWorkspace = workspaceRoot.trim() !== ''
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)

  return (
    <aside className="sidebar" aria-label="会话列表">
      <div className="sidebar__head">
        <span className="sidebar__heading">会话</span>
        <span className="sidebar__count">{sessions.length}</span>
        <button
          type="button"
          className="btn btn--ghost btn--icon"
          onClick={onRefresh}
          title="刷新列表"
          aria-label="刷新会话列表"
          disabled={loading}
        >
          <IconRefresh size={13} className={loading ? 'spin' : undefined} />
        </button>
      </div>

      {/* ---------- 工作区 ---------- */}
      <div className="sidebar__section">
        <div className="sidebar__section-head">
          <span className="sidebar__heading">工作区</span>
          {hasWorkspace && (
            <button
              type="button"
              className="btn btn--ghost btn--icon"
              onClick={onToggleFileSidebar}
              title={fileSidebarOpen ? '收起文件栏' : '展开文件栏'}
              aria-pressed={fileSidebarOpen}
            >
              <IconFolder size={13} />
            </button>
          )}
        </div>

        {hasWorkspace ? (
          <div className="ws">
            <div className="ws__path" title={workspaceRoot}>
              <IconFolder size={12} />
              <span className="ws__name">{workspaceRoot.split(/[\\/]/).filter(Boolean).pop()}</span>
            </div>
            <div className="ws__full" title={workspaceRoot}>
              {workspaceRoot}
            </div>
            <div className="ws__actions">
              <button
                type="button"
                className="btn btn--ghost ws__btn"
                onClick={onOpenFolder}
                disabled={folderPicking}
              >
                {folderPicking ? '等待选择…' : '换一个'}
              </button>
              <button
                type="button"
                className="btn btn--ghost ws__btn ws__btn--danger"
                onClick={onCloseFolder}
                title="关闭工作区（Agent 将不能再访问文件）"
              >
                关闭
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            className="ws__open"
            onClick={onOpenFolder}
            disabled={folderPicking}
          >
            <IconFolder size={13} />
            {folderPicking ? '等待你在系统对话框中选择…' : '打开文件夹'}
          </button>
        )}
      </div>

      <div className="sidebar__divider" />

      <div className="sidebar__list">
        {sessions.length === 0 ? (
          <div className="sidebar__empty">
            {loading ? '加载中…' : '还没有会话。新建一个会话，对话历史就会保存在服务端。'}
          </div>
        ) : (
          sessions.map((session) => {
            const active = session.id === activeId
            const unnamed = session.title.trim() === '' || session.title.includes('未命名')
            return (
              <div
                key={session.id}
                className="session-item"
                aria-current={active}
                role="button"
                tabIndex={0}
                onClick={() => onSelect(session.id)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onSelect(session.id)
                  }
                }}
              >
                <div className="session-item__body">
                  <span
                    className="session-item__title"
                    style={unnamed ? { color: 'var(--text-3)', fontStyle: 'italic' } : undefined}
                    title={session.title}
                  >
                    {session.title || '（未命名会话）'}
                  </span>
                  <span className="session-item__meta">
                    <span>{session.turn_count} 轮</span>
                    {session.total_tokens > 0 ? (
                      <span>· {formatCompact(session.total_tokens)} tok</span>
                    ) : null}
                    <span>· {formatRelativeTime(session.updated_at)}</span>
                  </span>
                </div>

                <button
                  type="button"
                  className="session-item__delete"
                  title={pendingDelete === session.id ? '再点一次确认删除' : '删除会话'}
                  aria-label="删除会话"
                  onClick={(event) => {
                    // 防止冒泡触发"选中会话"
                    event.stopPropagation()
                    if (pendingDelete === session.id) {
                      setPendingDelete(null)
                      onDelete(session.id)
                    } else {
                      setPendingDelete(session.id)
                    }
                  }}
                  onBlur={() => setPendingDelete(null)}
                  style={
                    pendingDelete === session.id
                      ? { opacity: 1, background: 'var(--danger-soft)', color: 'var(--danger)' }
                      : undefined
                  }
                >
                  <IconTrash size={12} />
                </button>
              </div>
            )
          })
        )}
      </div>

      <div className="sidebar__foot">
        <button type="button" className="btn btn--primary btn--block" onClick={onCreate}>
          <IconPlus size={13} />
          新建会话
        </button>
        <span className="muted mono" style={{ fontSize: 'var(--fs-xs)', textAlign: 'center' }}>
          {backend ? `存储：${backend}` : '存储：未知'}
        </span>
      </div>
    </aside>
  )
}
