/**
 * 工具调用卡片 —— 这个组件是整个界面里最能说明"Agent ≠ 聊天机器人"的部分。
 *
 * ============================================================
 * 为什么要把工具调用单独做成卡片
 * ============================================================
 * 普通聊天机器人的回答只能来自模型参数里的知识：它可能过时、可能算错、
 * 也可能一本正经地编造。Agent 的区别在于它能**主动获取信息**：
 *
 *     模型请求调用 → 你的进程真的执行了 → 结果被回灌给模型 → 模型据此再推理
 *
 * 一旦这个过程被隐藏，用户看到的就是一句"看起来更聪明的回答"，
 * 完全无法判断："它到底是查了数据，还是猜的？"
 *
 * 所以这张卡片要回答四个问题（对应后端的四个事件字段）：
 *   1. 调用了什么工具、参数是什么？   → tool_name / tool_args
 *   2. 成功了还是失败了？             → tool_ok
 *   3. 花了多久？                     → duration_ms
 *   4. 模型看到的是完整内容吗？        → truncated
 *
 * 第 4 点最容易被忽略，也最重要：工具输出会被服务端截断（默认 8000 字符），
 * 如果界面不体现，"模型漏答了文件后半部分"这类问题就完全无从定位 ——
 * 用户会以为是模型能力问题，实际是它根本没看见。
 */

import { useState } from 'react'
import type { ReactNode } from 'react'

import { formatDurationMs, looksLikeJson, summarizeToolArgs } from '../lib/format'
import type { ToolCallView } from '../lib/types'
import { IconCheck, IconScissors, IconSpinner, IconX } from './Icons'

function StatusIcon({ status }: { status: ToolCallView['status'] }): ReactNode {
  if (status === 'running') {
    return (
      <span className="toolcard__status" style={{ color: 'var(--accent)' }} title="执行中">
        <IconSpinner size={12} />
      </span>
    )
  }
  if (status === 'failed') {
    return (
      <span className="toolcard__status" style={{ color: 'var(--danger)' }} title="失败">
        <IconX size={13} />
      </span>
    )
  }
  return (
    <span className="toolcard__status" style={{ color: 'var(--success)' }} title="成功">
      <IconCheck size={13} />
    </span>
  )
}

export interface ToolCallCardProps {
  call: ToolCallView
}

/** JSON 输出格式化；不是 JSON 就原样返回（不做猜测式处理）。 */
function prettyOutput(text: string): string {
  if (!looksLikeJson(text)) return text
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

export function ToolCallCard({ call }: ToolCallCardProps) {
  // 失败的调用默认展开：用户的第一反应就是"为什么失败"，
  // 这时候还要他再点一次是多余的一步。成功的调用保持折叠，避免噪声。
  const [open, setOpen] = useState(call.status === 'failed')

  const argsText = Object.keys(call.args).length > 0 ? JSON.stringify(call.args, null, 2) : ''
  const summary = summarizeToolArgs(call.args)
  const isError = call.status === 'failed'

  return (
    <section className="toolcard" data-status={call.status}>
      <button
        type="button"
        className="toolcard__head"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <StatusIcon status={call.status} />
        <span className="toolcard__name">{call.name}</span>
        <span className="toolcard__args" title={summary}>
          {summary || (call.status === 'running' ? '调用中…' : '（无参数）')}
        </span>
        <span className="toolcard__stats">
          {call.truncated ? (
            <span
              className="toolcard__stat"
              style={{ color: 'var(--warn)' }}
              title="工具输出被截断：模型只看到了部分内容"
            >
              <IconScissors size={11} /> 已截断
            </span>
          ) : null}
          {call.durationMs !== null ? <span>{formatDurationMs(call.durationMs)}</span> : null}
          <span style={{ opacity: 0.7 }}>{open ? '收起' : '展开'}</span>
        </span>
      </button>

      {open ? (
        <div className="toolcard__body">
          <div>
            <div className="toolcard__section-label">参数</div>
            {argsText ? (
              <pre className="toolcard__pre">{argsText}</pre>
            ) : (
              <span className="toolcard__empty">该工具无需参数</span>
            )}
          </div>

          <div>
            <div className="toolcard__section-label">
              {isError ? '错误' : '观察结果（回灌给模型的内容）'}
            </div>
            {call.status === 'running' ? (
              <span className="toolcard__empty">执行中…</span>
            ) : call.output ? (
              <pre className={isError ? 'toolcard__pre toolcard__pre--error' : 'toolcard__pre'}>
                {/* 结构化输出（JSON）先格式化再显示：模型返回的 JSON 常常是单行，
                    直接贴出来会有几百字符的横向滚动，等于没显示。 */}
                {prettyOutput(call.output)}
              </pre>
            ) : (
              <span className="toolcard__empty">（空结果）</span>
            )}
          </div>

          {call.truncated ? (
            <div className="toolcard__empty">
              注意：本次输出超过了服务端上限（默认 8000 字符），已从中间截断 ——
              模型看到的不是完整内容，回答可能因此缺失细节。
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
