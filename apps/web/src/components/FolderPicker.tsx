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

import { useCallback, useEffect, useRef, useState } from 'react'

import { browseDirectories, locateFolder } from '../lib/settings-api'
import type { BrowseEntry, BrowseListing, LocateCandidate } from '../lib/types'
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
  // 系统对话框选完之后，服务端可能找到多个同名目录，需要用户确认
  const [candidates, setCandidates] = useState<LocateCandidate[] | null>(null)
  const [locating, setLocating] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

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
    setCandidates(null)
    setError(null)
    void go('')
  }, [open, go])

  /**
   * 处理"用系统对话框选"的结果。
   *
   * 【为什么只能拿到文件夹名】
   * `<input type="file" webkitdirectory>` 弹的**就是系统文件夹选择器**，
   * 但浏览器出于隐私**剥掉了绝对路径**：每个文件只有 `webkitRelativePath`
   * （形如 `MyAgent/src/App.tsx`）。任何网页都拿不到完整路径 ——
   * 这是浏览器的设计，不是能绕过去的实现细节。
   *
   * 所以这里把名字和几条相对路径交给服务端反查：
   * **名字用来筛选，结构用来确认。**
   */
  const handleNativePick = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      const first = files[0] as File & { webkitRelativePath?: string }
      const rel = first.webkitRelativePath ?? ''
      const name = rel.split('/')[0]
      if (!name) {
        setError('浏览器没有给出文件夹名，请改用下面的目录浏览。')
        return
      }

      // 取几条相对路径当"指纹"。分布取（首/中/尾）而不是只取前几条 ——
      // 同一个文件夹里前几个文件往往是同一类（比如一堆 README），
      // 打散了更能区分同名目录。
      const rels = Array.from(files)
        .map((f) => (f as File & { webkitRelativePath?: string }).webkitRelativePath ?? '')
        .filter(Boolean)
      const step = Math.max(1, Math.floor(rels.length / 6))
      const samples = rels
        .filter((_, i) => i % step === 0)
        .map((r) => r.split('/').slice(1).join('/'))
        .filter(Boolean)
        .slice(0, 6)

      setLocating(true)
      setError(null)
      setCandidates(null)
      try {
        const res = await locateFolder(name, samples)
        const only = res.candidates[0]
        if (res.candidates.length === 1 && only) {
          // 唯一匹配：直接选中，不再让用户点一次
          onPick(only.path)
        } else if (res.candidates.length > 1) {
          setCandidates(res.candidates)
        } else {
          setError(res.hint || `没有找到名为「${name}」的文件夹。`)
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : '定位失败')
      } finally {
        setLocating(false)
        // 清空 input，否则连续选同一个文件夹不会再触发 change
        if (fileInputRef.current) fileInputRef.current.value = ''
      }
    },
    [onPick],
  )

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

        {/* ---------- 主路径：系统文件夹对话框 ---------- */}
        <div className="picker__native">
          <button
            type="button"
            className="btn btn--primary btn--block"
            onClick={() => fileInputRef.current?.click()}
            disabled={locating}
          >
            {locating ? '正在定位…' : '用系统对话框选文件夹'}
          </button>
          {/*
            这里放的是一个**真实存在但隐藏**的 input。
            它弹出来的就是系统文件夹选择器 —— 但浏览器会剥掉绝对路径，
            所以选中之后要拿"文件夹名 + 几条相对路径"回来请服务端反查。
            这条限制写在下面的说明里，免得用户以为我们偷懒。
          */}
          <input
            ref={fileInputRef}
            type="file"
            // @ts-expect-error —— webkitdirectory 是事实标准但不在 React 的类型里
            webkitdirectory=""
            directory=""
            multiple
            style={{ display: 'none' }}
            onChange={(e) => void handleNativePick(e.target.files)}
          />
          <p className="picker__note picker__note--tight">
            系统对话框只肯给出文件夹**名字**（浏览器隐私限制，任何网页都一样）。
            我们会据名字和目录结构在你的磁盘上找回完整路径。
            <br />
            要是定位不到（或结果不对），用下面手动浏览 —— 那条路是**确定**的。
          </p>
        </div>

        {/* ---------- 多个同名目录时让用户确认 ---------- */}
        {candidates && candidates.length > 0 && (
          <div className="picker__candidates">
            <p className="picker__candidates-title">
              找到 {candidates.length} 个同名文件夹，请选实际的那一个：
            </p>
            <ul className="picker__list">
              {candidates.map((cand) => (
                <li key={cand.path}>
                  <div className="picker__entry">
                    <button
                      type="button"
                      className="picker__open"
                      onClick={() => onPick(cand.path)}
                      title={cand.path}
                    >
                      <IconFolder />
                      <span className="picker__name">{cand.path}</span>
                      {cand.matched > 0 && (
                        <span className="picker__count">{cand.matched} 项吻合</span>
                      )}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="picker__divider">或手动浏览</div>

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
