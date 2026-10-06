import { useEffect, useRef, useState } from 'react'
import { useDialogFocus } from '../hooks/useDialogFocus'
import { requestJson } from '../lib/api'
import { formatNumber } from '../lib/format'
import { describeStop } from '../lib/runtime'
import { describeRunEvent, runsPath } from '../lib/runs'
import type { RunList, RunRecord } from '../lib/runs'
import { AGENT_MODE_META } from '../lib/types'
import { IconRefresh, IconX } from './Icons'
import { Notice } from './Notice'

interface Props { open: boolean; initialId: string | null; sessionId: string | null; onClose: () => void }

export function RunHistoryDialog({ open, initialId, sessionId, onClose }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  useDialogFocus({ open, containerRef, onClose, initialFocusRef: closeRef })
  const [list, setList] = useState<RunList | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<RunRecord | null>(null)
  const [scope, setScope] = useState('all')
  const [reason, setReason] = useState('')
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const [loading, setLoading] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const deletion = useRef<AbortController | null>(null)
  const selectedRef = useRef(selected)
  selectedRef.current = selected

  useEffect(() => {
    if (open) { setSelected(initialId); setOffset(0); setScope('all'); setReason(''); setRevision((v) => v + 1) }
    return () => { deletion.current?.abort() }
  }, [open, initialId])

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setLoading(true); setError(null)
    void requestJson<RunList>(runsPath(offset, scope === 'session' ? sessionId : null, reason), { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setList(result) })
      .catch((e: unknown) => { if (!controller.signal.aborted) { setList(null); setError(e instanceof Error ? e.message : '读取运行记录失败') } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [open, offset, scope, sessionId, reason, revision])

  useEffect(() => {
    setDetail(null); setDetailError(null); setConfirmDelete(false)
    if (!open || !selected) { setDetailLoading(false); return }
    const controller = new AbortController()
    setDetailLoading(true)
    void requestJson<RunRecord>('/api/runs/' + encodeURIComponent(selected), { signal: controller.signal })
      .then((result) => { if (!controller.signal.aborted) setDetail(result) })
      .catch((e: unknown) => { if (!controller.signal.aborted) setDetailError(e instanceof Error ? e.message : '读取执行摘要失败') })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false) })
    return () => controller.abort()
  }, [open, selected, revision])

  const remove = async () => {
    if (!detail || (deletion.current && !deletion.current.signal.aborted)) return
    if (!confirmDelete) { setConfirmDelete(true); return }
    const controller = new AbortController()
    const id = detail.run_id
    deletion.current = controller
    setDeleting(true)
    try {
      await requestJson('/api/runs/' + encodeURIComponent(detail.run_id), { method: 'DELETE', signal: controller.signal })
      if (!controller.signal.aborted) { setSelected((current) => current === id ? null : current); setOffset(0); setRevision((v) => v + 1) }
    } catch (e) {
      if (!controller.signal.aborted && selectedRef.current === id) setDetailError(e instanceof Error ? e.message : '删除运行记录失败')
    } finally { if (deletion.current === controller) { deletion.current = null; setDeleting(false) } }
  }

  if (!open) return null
  return <div className="dialog-scrim" role="presentation" onClick={onClose}>
    <div className="dialog runs-dialog" role="dialog" aria-modal="true" aria-label="运行记录"
      ref={containerRef} tabIndex={-1} onClick={(e) => e.stopPropagation()}>
      <header className="dialog__head"><h2 className="dialog__title">运行记录</h2>
        <button type="button" className="btn btn--ghost btn--icon" ref={closeRef} aria-label="关闭运行记录" onClick={onClose}><IconX /></button>
      </header>
      <div className="runs-toolbar">
        <label>范围<select className="settings__input" aria-label="记录范围" value={scope}
          onChange={(e) => { setScope(e.target.value); setOffset(0); setSelected(null) }}>
          <option value="all">全部运行</option><option value="session" disabled={!sessionId}>当前会话</option>
        </select></label>
        <label>状态<select className="settings__input" aria-label="运行状态" value={reason}
          onChange={(e) => { setReason(e.target.value); setOffset(0); setSelected(null) }}>
          <option value="">全部状态</option>
          {['running', 'finished', 'error', 'cancelled', 'timeout', 'token_budget', 'max_steps', 'loop_detected', 'interrupted'].map((r) =>
            <option key={r} value={r}>{describeStop(r).text}</option>)}
        </select></label>
        <button type="button" className="btn" disabled={loading || detailLoading} onClick={() => setRevision((v) => v + 1)}><IconRefresh size={14} />刷新</button>
      </div>
      <p className="runs-note">仅执行摘要 · 不记录问题、答案、参数和结果原文。
        {list && (list.backend === 'memory' ? ' 当前为内存存储，服务重启后清空。' : ' 当前为 SQLite 持久存储。')}
      </p>
      {error && <Notice tone="error" title="运行记录暂不可用" text={error} />}
      <div className="runs-main">
        <nav className="runs-list" aria-label="任务运行列表">
          {loading && <p role="status">正在读取…</p>}
          {!loading && list?.runs.length === 0 && <p className="muted">没有符合条件的运行记录。</p>}
          {list?.runs.map((r) => <button type="button" key={r.run_id} className="runs-row" aria-pressed={selected === r.run_id}
            onClick={() => setSelected(r.run_id)}>
            <span className="runs-row__title">{AGENT_MODE_META[r.mode]?.label ?? r.mode}<span data-tone={describeStop(r.stopped_reason).tone}>{describeStop(r.stopped_reason).text}</span></span>
            <span>{new Date(r.started_at).toLocaleString('zh-CN', { hour12: false })}</span>
            <span>{r.stopped_reason === 'running' ? '等待结束' : `${r.duration_ms} ms`} · {r.tool_calls} 次工具请求{r.source === 'demo_replay' ? ' · 离线回放' : ''}</span>
            <code>{r.run_id.slice(0, 12)}</code>
          </button>)}
          {list && list.total > 50 && <div className="runs-pages">
            <button type="button" className="btn" disabled={loading || offset === 0} onClick={() => setOffset((v) => Math.max(0, v - 50))}>上一页</button>
            <span>{offset + 1}–{Math.min(offset + 50, list.total)} / {list.total}</span>
            <button type="button" className="btn" disabled={loading || offset + 50 >= list.total} onClick={() => setOffset((v) => v + 50)}>下一页</button>
          </div>}
        </nav>
        <section className="runs-detail" aria-label="执行摘要">
          {detailLoading ? <p role="status">正在读取执行摘要…</p> : !selected && <p className="muted">选择一轮任务，查看它的执行摘要。</p>}
          {detailError && <Notice tone="error" title="摘要操作失败" text={detailError} />}
          {detail && <>
            <h3>{AGENT_MODE_META[detail.mode]?.label ?? detail.mode} · {describeStop(detail.stopped_reason).text}</h3>
            <code className="runs-id">{detail.run_id}</code>
            <dl className="runs-stats">
              <div><dt>耗时</dt><dd>{detail.stopped_reason === 'running' ? '进行中' : detail.stopped_reason === 'interrupted' ? '无法确定' : `${detail.duration_ms} ms`}</dd></div>
              <div><dt>模型用量{!detail.usage_complete && '（已知）'}</dt><dd>{formatNumber(detail.usage.total_tokens)} tokens</dd></div>
              <div><dt>主任务步骤</dt><dd>{detail.steps_used}</dd></div>
              <div><dt>工具请求 / 返回 / 失败</dt><dd>{detail.tool_calls} / {detail.tool_results} / {detail.tool_failures}</dd></div>
            </dl>
            {!detail.usage_complete && <Notice tone="warn" title="用量统计不完整" text="仅显示已返回的用量，未知消耗未计入。" />}
            {detail.context_trimmed && <Notice tone="info" title="上下文已裁剪" text={`本轮上下文估算最多 ${detail.context_tokens} token。`} />}
            {detail.events_dropped > 0 && <Notice tone="warn" title="执行摘要已截断" text={`已省略 ${detail.events_dropped} 条后续事件，结束状态和累计统计仍保留。`} />}
            <h4>执行过程摘要</h4>
            <ol className="runs-timeline">{detail.events?.map((e, i) => <li key={i}>
              <span>+{e.elapsed_ms} ms{e.step > 0 && ` · 第 ${e.step} 步`}</span><p>{describeRunEvent(e)}</p>
            </li>)}</ol>
            <button type="button" className="btn" disabled={deleting || detail.stopped_reason === 'running'} onClick={() => void remove()}>
              {deleting ? '正在删除…' : confirmDelete ? '确认删除这条摘要' : '删除记录'}
            </button>
          </>}
        </section>
      </div>
    </div>
  </div>
}
