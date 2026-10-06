/**
 * 文件边栏（右侧常驻面板）：浏览工作区、预览文件。
 *
 * ============================================================
 * 为什么从"抽屉"改成"常驻边栏"
 * ============================================================
 * 第一版做成了 modal 抽屉（点开、遮住对话、看完关掉）。用起来立刻发现两个问题：
 *
 *   1. **它遮住的正是你要参照的东西。** 问 Agent"这个函数在哪定义的"，
 *      你要一边看文件树一边看回答 —— 而抽屉把对话盖住了。
 *   2. **每看一次要点两次。** 开、关，反复。
 *
 * 文件浏览不是"一个偶尔打开的功能"，而是**与对话并列的一等区域**。
 * 界面结构应该反映这一点：三列（会话 | 对话 | 文件），各占各的。
 *
 * 这也正是 DSH 这类工具的做法 —— 边栏常驻、可折叠，而不是弹窗。
 *
 * ============================================================
 * 与 Agent 的 read_file 共用同一条权限边界
 * ============================================================
 * 后端 `/api/files/*` 与 `app/tools/files.py` 复用同一个 `_resolve()`：
 * 解析符号链接后再校验、用 `is_relative_to` 比路径分量、敏感文件黑名单。
 *
 * 如果界面走一套宽松的校验，用户就能在这里点开 Agent 侧明确拒绝读取的 `.env` ——
 * **同一个系统里两套权限判定，等于没有权限判定。**
 */

import { useCallback, useLayoutEffect, useRef, useState } from 'react'

import { useDialogFocus } from '../hooks/useDialogFocus'
import { listDirectory, readFileContent } from '../lib/settings-api'
import type { DirListing, FileContent } from '../lib/types'
import { Markdown } from './Markdown'
import { IconChevronRight, IconFile, IconFolder, IconRefresh, IconX } from './Icons'

export interface FileSidebarProps {
  /** 工作区根目录。空字符串 = 未配置 */
  root: string
  onClose: () => void
  /** 未配置时引导去打开文件夹 */
  onOpenFolder: () => void
  /** 换一个工作区 */
  onSwitchFolder: () => void
  /** 窄屏作为模态抽屉；桌面并列面板不限制焦点。 */
  modal?: boolean
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function FileSidebar({ root, onClose, onOpenFolder, onSwitchFolder, modal = false }: FileSidebarProps) {
  const configured = root.trim() !== ''
  const containerRef = useRef<HTMLElement | null>(null)
  const closeRef = useRef<HTMLButtonElement | null>(null)
  const directoryRequest = useRef(0)
  const previewRequest = useRef(0)
  const currentRoot = useRef(root)
  useDialogFocus({ open: true, containerRef, onClose, initialFocusRef: closeRef, modal })

  const [listing, setListing] = useState<DirListing | null>(null)
  const [preview, setPreview] = useState<FileContent | null>(null)
  const [loading, setLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showHidden, setShowHidden] = useState(false)

  const browse = useCallback(
    async (path: string) => {
      const request = ++directoryRequest.current
      setLoading(true)
      setListing(null)
      setError(null)
      try {
        const next = await listDirectory(path, showHidden)
        if (request === directoryRequest.current && currentRoot.current === root) setListing(next)
      } catch (err) {
        if (request === directoryRequest.current && currentRoot.current === root) {
          setError(err instanceof Error ? err.message : '读取目录失败')
        }
      } finally {
        if (request === directoryRequest.current && currentRoot.current === root) setLoading(false)
      }
    },
    [root, showHidden],
  )

  // 工作区变了（或切换隐藏文件）就回到根目录重来 ——
  // 保留旧路径会指向一个在新工作区里不存在的相对路径，必然报错
  useLayoutEffect(() => {
    // 新根目录首次绘制前清掉旧预览；旧请求的返回值也必须匹配该根目录。
    currentRoot.current = root
    directoryRequest.current += 1
    previewRequest.current += 1
    setListing(null)
    setPreview(null)
    setError(null)
    setLoading(false)
    setPreviewLoading(false)
    if (configured) void browse('.')
    return () => {
      // 请求仍可返回，但关闭面板或切换根目录后不能提交到组件状态。
      directoryRequest.current += 1
      previewRequest.current += 1
    }
  }, [configured, root, browse])

  const openFile = async (path: string) => {
    const request = ++previewRequest.current
    setError(null)
    setPreview(null)
    setPreviewLoading(true)
    try {
      const next = await readFileContent(path)
      if (request === previewRequest.current && currentRoot.current === root) setPreview(next)
    } catch (err) {
      if (request === previewRequest.current && currentRoot.current === root) {
        setError(err instanceof Error ? err.message : '读取文件失败')
      }
    } finally {
      if (request === previewRequest.current && currentRoot.current === root) setPreviewLoading(false)
    }
  }

  const goUp = () => {
    if (!listing) return
    const parts = listing.path.split('/').filter(Boolean)
    parts.pop()
    void browse(parts.join('/') || '.')
  }

  return (
    <aside
      ref={containerRef}
      className={modal ? 'fileside fileside--modal' : 'fileside'}
      aria-label="工作区文件"
      role={modal ? 'dialog' : undefined}
      aria-modal={modal ? true : undefined}
      aria-busy={loading || previewLoading}
      tabIndex={modal ? -1 : undefined}
    >
      <div className="fileside__head">
        <span className="fileside__heading">
          <IconFolder size={13} /> 文件
        </span>
        {configured && (
          <>
            <button
              type="button"
              className="btn btn--ghost btn--icon"
              onClick={onSwitchFolder}
              title="换一个工作区文件夹"
              aria-label="切换工作区文件夹"
            >
              <IconFolder size={13} />
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--icon"
              onClick={() => void browse(listing?.path ?? '.')}
              title="刷新"
              aria-label="刷新工作区文件"
            >
              <IconRefresh size={13} />
            </button>
          </>
        )}
        <button
          ref={closeRef}
          type="button"
          className="btn btn--ghost btn--icon"
          onClick={onClose}
          title="收起文件栏"
          aria-label="收起文件栏"
        >
          <IconX size={13} />
        </button>
      </div>

      {!configured ? (
        /* 未配置时给引导，而不是显示一个空目录 ——
           空目录与"没配置"在界面上长得一样，但原因完全不同 */
        <div className="fileside__empty">
          <p>还没有打开文件夹。</p>
          <p className="settings__hint settings__hint--block">
            选择文件夹后，就能浏览和预览其中的文件。Agent 只访问你明确授权的工作区。
          </p>
          <button type="button" className="btn btn--primary btn--block" onClick={onOpenFolder}>
            打开文件夹
          </button>
        </div>
      ) : (
        <>
          <div className="fileside__root" title={root}>
            {root}
          </div>

          <div className="fileside__crumbs">
            <button
              type="button"
              className="btn btn--ghost fileside__crumb"
              onClick={() => void browse('.')}
              disabled={listing?.path === '.'}
            >
              根
            </button>
            {listing?.path
              .split('/')
              .filter(Boolean)
              .map((segment, index, all) => (
                <span key={`${segment}-${index}`} className="fileside__crumb-group">
                  <IconChevronRight size={11} />
                  <button
                    type="button"
                    className="btn btn--ghost fileside__crumb"
                    onClick={() => void browse(all.slice(0, index + 1).join('/'))}
                    disabled={index === all.length - 1}
                  >
                    {segment}
                  </button>
                </span>
              ))}
          </div>

          <label className="fileside__toggle">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => setShowHidden(e.target.checked)}
            />
            显示隐藏文件
          </label>

          <div className="fileside__scroll">
            {loading && <p className="drawer__note">读取中…</p>}
            {previewLoading && <p className="drawer__note" role="status">正在读取文件…</p>}
            {error && <p className="settings__alert settings__alert--bad">{error}</p>}

            <ul className="fileside__entries">
              {listing?.can_go_up && (
                <li>
                  <button type="button" className="fileside__entry" onClick={goUp}>
                    <IconFolder size={12} />
                    <span className="fileside__name">..</span>
                  </button>
                </li>
              )}
              {listing?.entries.map((entry) => (
                <li key={entry.path}>
                  <button
                    type="button"
                    className={
                      preview?.path === entry.path
                        ? 'fileside__entry fileside__entry--active'
                        : 'fileside__entry'
                    }
                    onClick={() => (entry.is_dir ? void browse(entry.path) : void openFile(entry.path))}
                    title={entry.path}
                  >
                    {entry.is_dir ? <IconFolder size={12} /> : <IconFile size={12} />}
                    <span className="fileside__name">{entry.name}</span>
                    {!entry.is_dir && <span className="fileside__size">{formatSize(entry.size)}</span>}
                  </button>
                </li>
              ))}
            </ul>

            {listing && listing.entries.length === 0 && (
              <p className="drawer__note">这个目录是空的</p>
            )}
            {listing?.truncated && <p className="drawer__note">目录结果已截断，请进入子目录查看。</p>}

            {/* 预览紧跟在列表下面，而不是并排 ——
                边栏本来就窄，再分两栏两边都会挤到不可读 */}
            {preview && (
              <div className="fileside__preview">
                <div className="fileside__preview-head">
                  <code>{preview.path}</code>
                  <span className="fileside__size">{formatSize(preview.size)}</span>
                </div>
                {preview.is_binary ? (
                  <p className="drawer__note">二进制文件，无法作为文本预览。</p>
                ) : preview.path.toLowerCase().endsWith('.md') ? (
                  <div className="fileside__markdown">
                    <Markdown source={preview.content} />
                  </div>
                ) : (
                  <pre className="fileside__code">
                    <code>{preview.content}</code>
                  </pre>
                )}
                {preview.truncated && <p className="drawer__note">内容已截断（超出服务端上限）</p>}
              </div>
            )}
          </div>
        </>
      )}
    </aside>
  )
}
