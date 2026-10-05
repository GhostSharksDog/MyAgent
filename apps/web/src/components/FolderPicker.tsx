/**
 * 目录选择器：浏览服务端文件系统、挑一个目录作为工作区。
 *
 * ============================================================
 * 为什么这个东西必须存在，而且必须能走出工作区
 * ============================================================
 * 浏览器**没有服务端的文件夹对话框**。`<input type="file" webkitdirectory>`
 * 只能拿到文件、拿不到路径 —— 那是浏览器的安全设计，改不了。
 *
 * 所以"打开文件夹作为工作区"这件事只能由服务端列目录来支持。
 * 而这里有个绕不开的循环依赖：要选工作区，就必须能浏览工作区**之外**的目录，
 * 否则用户永远只能选当前工作区里面的文件夹。
 *
 * 于是这一条成了**刻意放宽、又刻意收窄**的权限：
 *     放宽：可以列出任意绝对路径下的目录结构
 *     收窄：**只列目录，不列文件、不读内容**
 *
 * 界就是"选择工作区需要看到目录名，但不需要看到文件内容"。
 * 一句话能说清的权限，才不会在后续改动里被悄悄放宽。
 *
 * ============================================================
 * 为什么不用"输入路径"就够了
 * ============================================================
 * 设置面板里确实有一个路径输入框。但让人**凭记忆手打一个绝对路径**
 * 是很容易出错的：`D:/WXP/简历/MyAgent` 少一层、斜杠方向写反、
 * 中文字符 …… 而错了之后的表现是"文件功能没反应"，
 * 用户要对着错误提示猜自己哪里打错了。
 *
 * **能点就不要让人打。** 手输作为兜底保留（粘贴路径更快），
 * 但主路径应该是点选。
 */

import { useCallback, useEffect, useState } from 'react'

import { browseDirectories } from '../lib/settings-api'
import type { BrowseEntry, BrowseListing } from '../lib/types'
import { IconChevronRight, IconFolder, IconX } from './Icons'

export interface FolderPickerProps {
  open: boolean
  onClose: () => void
  onPick: (absolutePath: string) => void
  /** 当前已配置的工作区，用于高亮"当前" */
  current?: string
}

export function FolderPicker({ open, onClose, onPick, current }: FolderPickerProps) {
  const [listing, setListing] = useState<BrowseListing | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const go = useCallback(async (path: string) => {
    setLoading(true)
    setError(null)
    try {
      setListing(await browseDirectories(path))
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取目录失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 每次打开都从起点重新开始（而不是接着上次的位置）——
  // 上一次的浏览位置对这一次没有意义，而"打开后停在一个奇怪的地方"
  // 会让人怀疑是不是坏了
  useEffect(() => {
    if (!open) return
    setListing(null)
    void go('')
  }, [open, go])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const segments = (listing?.path ?? '').split(/[\\/]/).filter(Boolean)
  const showingRoots = listing != null && listing.path === ''

  return (
    <div className="drawer-scrim" onClick={onClose} role="presentation">
      <div
        className="picker"
        role="dialog"
        aria-modal="true"
        aria-label="选择工作区文件夹"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="picker__head">
          <h2 className="picker__title">
            <IconFolder /> 打开文件夹作为工作区
          </h2>
          <button type="button" className="btn btn--ghost btn--icon" onClick={onClose} title="关闭（Esc）">
            <IconX />
          </button>
        </header>

        <p className="picker__note">
          Agent 只能读写你选中的这个目录**以内**的文件。这里只显示目录名，不显示文件内容。
        </p>

        {/* 路径栏：显示当前位置，也允许直接粘贴一个绝对路径 */}
        <div className="picker__path">
          <button
            type="button"
            className="btn btn--ghost picker__crumb"
            onClick={() => void go('')}
            disabled={showingRoots}
          >
            起点
          </button>
          {segments.map((segment, index) => (
            <span key={`${segment}-${index}`} className="picker__crumb-group">
              <IconChevronRight size={12} />
              <span className="picker__crumb-text">{segment}</span>
            </span>
          ))}
        </div>

        <div className="picker__body">
          {loading && <p className="drawer__note">读取中…</p>}
          {error && <p className="settings__alert settings__alert--bad">{error}</p>}

          {!loading && listing && (
            <ul className="picker__list">
              {listing.parent && (
                <li>
                  <button type="button" className="picker__entry" onClick={() => void go(listing.parent!)}>
                    <IconFolder />
                    <span className="picker__name">..</span>
                  </button>
                </li>
              )}
              {(showingRoots ? listing.roots : listing.entries).map((entry) => (
                <PickerRow
                  key={entry.path}
                  entry={entry}
                  isCurrent={current === entry.path}
                  onOpen={() => void go(entry.path)}
                  onPick={() => onPick(entry.path)}
                />
              ))}
            </ul>
          )}

          {!loading && listing && !showingRoots && listing.entries.length === 0 && (
            <p className="drawer__note">这个目录下没有子目录</p>
          )}
        </div>

        <footer className="picker__foot">
          <span className="picker__current">
            {listing?.path ? (
              <>
                当前浏览：<code>{listing.path}</code>
              </>
            ) : (
              '选择一个位置开始'
            )}
          </span>
          <div className="picker__actions">
            <button type="button" className="btn btn--ghost" onClick={onClose}>
              取消
            </button>
            <button
              type="button"
              className="btn btn--primary"
              disabled={!listing?.path}
              onClick={() => listing?.path && onPick(listing.path)}
            >
              就用这个目录
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}

/** 一行目录。**单击进入、按钮选中** —— 两个动作必须分开。 */
function PickerRow({
  entry,
  isCurrent,
  onOpen,
  onPick,
}: {
  entry: BrowseEntry
  isCurrent: boolean
  onOpen: () => void
  onPick: () => void
}) {
  return (
    <li>
      <div className={isCurrent ? 'picker__entry picker__entry--current' : 'picker__entry'}>
        {/* 点名字 = 进去看；点右侧按钮 = 就选它。
            合成一个动作的话，用户想"进去确认一下"就变成了"直接选中"。 */}
        <button type="button" className="picker__open" onClick={onOpen} title="进入这个目录">
          <IconFolder />
          <span className="picker__name">{entry.name}</span>
          {entry.child_count > 0 && (
            <span className="picker__count">{entry.child_count}</span>
          )}
        </button>
        <button type="button" className="btn btn--ghost picker__pick" onClick={onPick}>
          选它
        </button>
      </div>
    </li>
  )
}
