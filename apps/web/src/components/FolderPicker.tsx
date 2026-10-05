/**
 * 目录选择器：把"打开文件夹作为工作区"这件事做对。
 *
 * ============================================================
 * 这里有两套交互，按**服务端声明的能力**二选一
 * ============================================================
 *
 *   native —— 宿主进程能弹出**系统**对话框。界面就是一个按钮，
 *             点完直接拿回绝对路径，一步到位。
 *   browse —— 宿主弹不出（远程浏览器访问、没有图形会话……）。
 *             界面变成应用内的目录浏览面板。
 *
 * 为什么不统一成一种？因为它们的适用条件不同，而**代价也是不对等的**：
 *
 *   浏览器自己弹不出返回绝对路径的对话框 —— `<input type="file" webkitdirectory>`
 *   弹的确实是系统选择器，但拿到的每个文件只有 `webkitRelativePath`
 *   （形如 `MyAgent/src/App.tsx`），**绝对路径被浏览器剥掉了**。
 *   （File System Access API 也一样：它只给目录 handle，`handle.name` 是名字。）
 *
 * 所以"用浏览器对话框"这条路只能靠猜：拿名字去磁盘上反查。
 * 有系统对话框时，那条路完全没必要 —— 它是兜底，不是主路。
 *
 * ============================================================
 * 为什么不用"输入路径"就够了
 * ============================================================
 * 让人**凭记忆手打一个绝对路径**很容易出错：`D:/WXP/简历/MyAgent` 少一层、
 * 斜杠写反、中文字符……而错了之后的表现是"文件功能没反应"，
 * 用户只能对着错误提示猜自己哪里打错了。**能点就不要让人打。**
 * 手输作为兜底保留（粘贴路径更快），但主路径是点选。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { resolvePickerView } from '../lib/picker'
import type { PickerView } from '../lib/picker'
import {
  browseDirectories,
  fetchPickerCapability,
  locateFolder,
  pickDirectory,
} from '../lib/settings-api'
import type { BrowseEntry, BrowseListing, LocateCandidate, PickerInfo } from '../lib/types'
import { IconChevronRight, IconFolder, IconX } from './Icons'

export interface FolderPickerProps {
  open: boolean
  onClose: () => void
  onPick: (absolutePath: string) => void
  /** 当前已配置的工作区，用于高亮"当前" */
  current?: string
  /**
   * 由调用方带进来的一句话：**为什么没直接用系统对话框**。
   *
   * 【为什么这句话必须由外面传进来，而不是面板自己推断】
   * 面板是被"退回来"的 —— 决定用它的是上一步（点"打开文件夹"的那一下）：
   * 宿主的 `capability` 说它弹不出，或者弹的过程中失败了。那两种原因
   * 只有上一步知道；面板自己去猜只能猜出"没有对话框"，而说不出是为什么。
   * 而"为什么"正是用户此刻唯一想知道的。
   */
  note?: string
}

export function FolderPicker({ open, onClose, onPick, current, note }: FolderPickerProps) {
  const [listing, setListing] = useState<BrowseListing | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // 系统对话框选完之后，服务端可能找到多个同名目录，需要用户确认（仅 browse 路径）
  const [candidates, setCandidates] = useState<LocateCandidate[] | null>(null)
  const [locating, setLocating] = useState(false)

  // 能力：服务端说它能弹系统对话框，还是只能用应用内浏览
  const [capability, setCapability] = useState<PickerInfo | null>(null)
  const [capabilityError, setCapabilityError] = useState<string | null>(null)
  // 系统对话框正开着（等用户操作）—— 这个状态可能持续几分钟
  const [waitingDialog, setWaitingDialog] = useState(false)

  const fileInputRef = useRef<HTMLInputElement>(null)
  const view = resolvePickerView(capability, capabilityError)

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
    setCapability(null)
    setCapabilityError(null)
    void go('')

    // 能力也重新问一次：服务可能在这期间换了绑定地址或重启过
    let alive = true
    fetchPickerCapability()
      .then((info) => {
        if (alive) setCapability(info)
      })
      .catch((err: unknown) => {
        if (alive) setCapabilityError(err instanceof Error ? err.message : '无法确认目录选择能力')
      })
    return () => {
      alive = false
    }
  }, [open, go])

  /**
   * 主路径：请宿主进程弹出**系统**对话框。
   *
   * 【为什么这里要一直等到用户点完】
   * 这个请求是"用户节奏"的，可能要挂几分钟。等待期间按钮要禁用、
   * 并且明确告诉用户"对话框已经弹出来了" —— 否则用户会以为卡住了
   * 而反复点击（第二次点击会被服务端拒绝：同一个时刻只允许一个对话框）。
   */
  const handleServerDialog = useCallback(async () => {
    setWaitingDialog(true)
    setError(null)
    setCandidates(null)
    try {
      const result = await pickDirectory()
      if (result.path) {
        onPick(result.path)
      } else {
        setError(result.hint || '已取消选择。')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '打开系统对话框失败')
    } finally {
      setWaitingDialog(false)
    }
  }, [onPick])

  /**
   * 兜底路径：用浏览器自己的文件夹选择器。
   *
   * 【为什么只能拿到文件夹名】
   * `<input type="file" webkitdirectory>` 弹的**就是系统文件夹选择器**，
   * 但浏览器出于隐私**剥掉了绝对路径**：每个文件只有 `webkitRelativePath`
   * （形如 `MyAgent/src/App.tsx`），而且只有 Chromium 系支持。
   *
   * 所以这里把名字和几条相对路径交给服务端反查：
   * **名字用来筛选，结构用来确认。**
   */
  const handleBrowserPick = useCallback(
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
      // 系统对话框开着的时候，Esc 应该关的是那个对话框，而不是这个面板
      if (event.key === 'Escape' && !waitingDialog) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose, waitingDialog])

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

        {/* ---------- 主路径：由宿主进程弹出系统对话框 ---------- */}
        <PickerPrimary
          view={view}
          note={note}
          waiting={waitingDialog}
          locating={locating}
          onServerDialog={() => void handleServerDialog()}
          onBrowserDialog={() => fileInputRef.current?.click()}
        />

        {/* 浏览器选择器只在 browse 模式下才存在（native 模式下它是多余的） */}
        {view.interaction === 'browse' && (
          <input
            ref={fileInputRef}
            type="file"
            // @ts-expect-error —— webkitdirectory 是事实标准但不在 React 的类型里
            webkitdirectory=""
            directory=""
            multiple
            style={{ display: 'none' }}
            onChange={(e) => void handleBrowserPick(e.target.files)}
          />
        )}

        {/* ---------- 多个同名目录时让用户确认（仅 browse 路径） ---------- */}
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

/**
 * 主路径区块：按 `view.interaction` 渲染三种形态之一。
 *
 * 【为什么不"能弹就弹、不能弹再降级"】
 * 降级（先渲染按钮，失败后换成面板）听起来更"自适应"，实际是把一个
 * **启动时就确定的静态事实**变成了一次失败的用户操作。先问能力、
 * 再渲染正确的形态，用户永远不会看到"点了一个没反应的按钮"。
 */
function PickerPrimary({
  view,
  note,
  waiting,
  locating,
  onServerDialog,
  onBrowserDialog,
}: {
  view: PickerView
  note?: string
  waiting: boolean
  locating: boolean
  onServerDialog: () => void
  onBrowserDialog: () => void
}) {
  // 【为什么优先用外面传进来的 note】
  // 它是**上一步真正失败/退让的原因**（"服务绑定在 0.0.0.0，对话框会开在你看不到的
  // 屏幕上"），比这里能推断出的通用说法具体得多。而用户此刻唯一想知道的就是原因。
  const reason = note || view.reason

  if (view.status === 'unavailable') {
    return (
      <div className="picker__native">
        <p className="settings__alert settings__alert--bad">{reason}</p>
        <p className="picker__note picker__note--tight">
          下面的手动浏览目录仍然可用 —— 它不依赖系统对话框。
        </p>
      </div>
    )
  }

  if (view.status === 'loading') {
    return (
      <div className="picker__native">
        <p className="drawer__note">{reason}</p>
      </div>
    )
  }

  if (view.interaction === 'native') {
    return (
      <div className="picker__native">
        <button
          type="button"
          className="btn btn--primary btn--block"
          onClick={onServerDialog}
          disabled={waiting}
        >
          {waiting ? '等待你在系统对话框中选择…' : '用系统对话框选文件夹'}
        </button>
        <p className="picker__note picker__note--tight">
          {waiting ? (
            <>
              系统对话框已经弹出 —— 它在你的**桌面**上（可能被浏览器挡在后面）。
              选好文件夹后这里会自动填上完整路径。
            </>
          ) : (
            <>
              对话框由**本机的 Legacy 服务**弹出，因此能拿到**完整路径** ——
              浏览器自己做不到这一点（它只会给你文件夹的名字）。
            </>
          )}
        </p>
      </div>
    )
  }

  return (
    <div className="picker__native">
      <button
        type="button"
        className="btn btn--primary btn--block"
        onClick={onBrowserDialog}
        disabled={locating}
      >
        {locating ? '正在定位…' : '用浏览器选择文件夹'}
      </button>
      <p className="picker__note picker__note--tight">
        {reason}
        <br />
        浏览器只肯给出文件夹**名字**（这是它的隐私设计），所以我们会据名字和目录结构
        在你的磁盘上找回完整路径。定位不到时，用下面手动浏览 —— 那条路是**确定**的。
      </p>
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
