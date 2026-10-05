/**
 * 文件浏览器（右侧抽屉）：浏览工作区、预览文件内容。
 *
 * ============================================================
 * 这个面板与 Agent 的 `read_file` 工具**共用同一条权限边界**
 * ============================================================
 * 后端的 `/api/files/*` 与 `app/tools/files.py` 复用同一个 `_resolve()`：
 * 解析符号链接后再校验、用 `is_relative_to` 比路径分量、敏感文件黑名单。
 *
 * 这不是实现细节上的偷懒，而是**正确性要求**：如果界面走一套宽松的校验，
 * 用户就能在预览面板里点开 Agent 侧明确拒绝读取的 `.env` ——
 * 同一个系统里两套权限判定，等于没有权限判定。
 * 所以下面这些接口调用看不见任何"绕过"逻辑，因为不该有。
 *
 * ============================================================
 * 为什么"未配置工作区"要给引导而不是显示空目录
 * ============================================================
 * 空目录与"没配置"在界面上长得一模一样，但原因完全不同：
 * 前者是"这个目录里真的没东西"，后者是"你还没告诉我去哪看"。
 * 混在一起的表现是用户对着空面板困惑 —— 而这正是 P6 要修的那类问题。
 * 所以未配置时直接给一段说明 + 指向设置面板。
 *
 * ============================================================
 * 为什么用"当前目录 + 面包屑"而不是完整递归树
 * ============================================================
 * 递归树要先遍历整个工作区：一个 node_modules 就能有几十万个文件，
 * 首屏会卡死。按需展开（点一层、取一层）的代价是每次点击一次请求，
 * 但那个代价是**用户可感知且可控**的，而首屏卡死的代价是不可控的。
 * 两个都不完美时，选"慢但看得见"的那个。
 */

import { useCallback, useEffect, useState } from 'react'

import { listDirectory, readFileContent } from '../lib/settings-api'
import type { DirListing, FileContent } from '../lib/types'
import { Markdown } from './Markdown'
import { IconChevronRight, IconFile, IconFolder, IconRefresh, IconX } from './Icons'

export interface FileBrowserProps {
  open: boolean
  onClose: () => void
  /** 工作区是否已配置（来自设置面板的数据，避免重复请求） */
  configured: boolean
  root: string
  /** 未配置时引导用户去设置面板 */
  onOpenSettings: () => void
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export function FileBrowser({ open, onClose, configured, root, onOpenSettings }: FileBrowserProps) {
  const [listing, setListing] = useState<DirListing | null>(null)
  const [preview, setPreview] = useState<FileContent | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showHidden, setShowHidden] = useState(false)

  const browse = useCallback(
    async (path: string) => {
      setLoading(true)
      setError(null)
      setPreview(null)
      try {
        setListing(await listDirectory(path, showHidden))
      } catch (err) {
        setError(err instanceof Error ? err.message : '读取目录失败')
      } finally {
        setLoading(false)
      }
    },
    [showHidden],
  )

  // 打开时从根目录开始；切换"显示隐藏文件"时刷新当前目录
  useEffect(() => {
    if (!open || !configured) return
    void browse(listing?.path ?? '.')
    // 刻意不依赖 listing：那样每次列举都会再触发一次列举，变成死循环
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, configured, showHidden, browse])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const openFile = async (path: string) => {
    setError(null)
    try {
      setPreview(await readFileContent(path))
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取文件失败')
    }
  }

  const goUp = () => {
    if (!listing) return
    const parts = listing.path.split('/').filter(Boolean)
    parts.pop()
    void browse(parts.join('/') || '.')
  }

  return (
    <div className="drawer-scrim" onClick={onClose} role="presentation">
      <aside
        className="drawer drawer--wide"
        role="dialog"
        aria-modal="true"
        aria-label="文件浏览"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="drawer__head">
          <div>
            <h2 className="drawer__title">
              <IconFolder /> 文件
            </h2>
            <p className="drawer__subtitle">
              {configured ? <>工作区：{root}</> : '尚未配置工作区'}
            </p>
          </div>
          <div className="files__head-actions">
            {configured && (
              <>
                <label className="files__toggle">
                  <input
                    type="checkbox"
                    checked={showHidden}
                    onChange={(e) => setShowHidden(e.target.checked)}
                  />
                  显示隐藏文件
                </label>
                <button
                  type="button"
                  className="btn btn--ghost btn--icon"
                  onClick={() => void browse(listing?.path ?? '.')}
                  title="刷新"
                >
                  <IconRefresh />
                </button>
              </>
            )}
            <button type="button" className="btn btn--ghost btn--icon" onClick={onClose} title="关闭（Esc）">
              <IconX />
            </button>
          </div>
        </header>

        <div className="drawer__body drawer__body--files">
          {!configured ? (
            /* 未配置时给引导，而不是显示一个空目录 ——
               两者在界面上长得一样，但原因完全不同 */
            <div className="files__empty">
              <p>文件功能未启用。</p>
              <p className="settings__hint settings__hint--block">
                Agent 只能访问你**显式指定**的目录。在设置里填一个「工作区根目录」即可启用，
                路径穿越与符号链接逃逸都会被拦下。
              </p>
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => {
                  onClose()
                  onOpenSettings()
                }}
              >
                去设置里配置
              </button>
            </div>
          ) : (
            <div className="files__split">
              {/* ---------- 左：目录列表 ---------- */}
              <div className="files__list">
                <div className="files__crumbs">
                  <button
                    type="button"
                    className="btn btn--ghost files__crumb"
                    onClick={() => void browse('.')}
                    disabled={listing?.path === '.'}
                  >
                    根目录
                  </button>
                  {listing?.path
                    .split('/')
                    .filter(Boolean)
                    .map((segment, index, all) => (
                      <span key={`${segment}-${index}`} className="files__crumb-group">
                        <IconChevronRight size={12} />
                        <button
                          type="button"
                          className="btn btn--ghost files__crumb"
                          onClick={() => void browse(all.slice(0, index + 1).join('/'))}
                          disabled={index === all.length - 1}
                        >
                          {segment}
                        </button>
                      </span>
                    ))}
                </div>

                {loading && <p className="drawer__note">读取中…</p>}

                <ul className="files__entries">
                  {listing?.can_go_up && (
                    <li>
                      <button type="button" className="files__entry" onClick={goUp}>
                        <IconFolder />
                        <span className="files__name">..</span>
                      </button>
                    </li>
                  )}
                  {listing?.entries.map((entry) => (
                    <li key={entry.path}>
                      <button
                        type="button"
                        className={
                          preview?.path === entry.path ? 'files__entry files__entry--active' : 'files__entry'
                        }
                        onClick={() =>
                          entry.is_dir ? void browse(entry.path) : void openFile(entry.path)
                        }
                        title={entry.path}
                      >
                        {entry.is_dir ? <IconFolder /> : <IconFile />}
                        <span className="files__name">{entry.name}</span>
                        {!entry.is_dir && <span className="files__size">{formatSize(entry.size)}</span>}
                      </button>
                    </li>
                  ))}
                </ul>

                {listing && listing.entries.length === 0 && (
                  <p className="drawer__note">这个目录是空的</p>
                )}
                {listing?.truncated && (
                  <p className="drawer__note">条目过多，只显示了一部分</p>
                )}
              </div>

              {/* ---------- 右：预览 ---------- */}
              <div className="files__preview">
                {!preview && !error && (
                  <p className="drawer__note">选择左侧的文件即可预览</p>
                )}
                {error && <p className="settings__alert settings__alert--bad">{error}</p>}
                {preview && (
                  <>
                    <div className="files__preview-head">
                      <code>{preview.path}</code>
                      <span className="files__size">{formatSize(preview.size)}</span>
                    </div>
                    {preview.is_binary ? (
                      <p className="drawer__note">这是二进制文件，无法作为文本预览。</p>
                    ) : preview.path.toLowerCase().endsWith('.md') ? (
                      /* Markdown 走渲染而不是 <pre> —— 这是"预览"而不是"看原文"。
                         想看原文的用户可以滚到下面看代码块？不：这里只有一个视图，
                         因为预览场景下渲染后的可读性远比看源码重要。 */
                      <div className="files__markdown">
                        <Markdown source={preview.content} />
                      </div>
                    ) : (
                      <pre className="files__code">
                        <code>{preview.content}</code>
                      </pre>
                    )}
                    {preview.truncated && (
                      <p className="drawer__note">内容已截断（超出服务端上限）</p>
                    )}
                  </>
                )}
              </div>
            </div>
          )}
        </div>
      </aside>
    </div>
  )
}
