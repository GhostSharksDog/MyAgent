/**
 * 输入区。
 *
 * 几个刻意的行为：
 *   1. **Enter 发送、Shift+Enter 换行**（并支持 Ctrl/Cmd+Enter 也发送）。
 *      中文输入法下 Enter 是"确认候选词"，所以必须检查 composition 事件 ——
 *      不检查的话，打字打到一半按回车会把半成品发出去。
 *      这是中文前端最常见、也最容易被忽略的 bug。
 *   2. **高度自适应**（最多 220px 后内部滚动），而不是固定两行或无限长高。
 *   3. 流式过程中输入框保持可用但发送键变成"停止"：
 *      用户随时能中断，中断按钮会真的断开 HTTP 连接（后端随之停止后续步骤）。
 */

import { useEffect, useRef } from 'react'
import type { KeyboardEvent } from 'react'

import { AGENT_MODE_META } from '../lib/types'
import type { AgentMode } from '../lib/types'
import { IconArrowUp, IconStop } from './Icons'

export interface ComposerProps {
  value: string
  onChange: (value: string) => void
  onSend: () => void
  onStop: () => void
  streaming: boolean
  disabled?: boolean
  /** 可用的 Agent 形态。**由后端 `/api/meta` 提供**，不是前端硬编码的列表。 */
  modes: AgentMode[]
  current: AgentMode
  onModeChange: (mode: AgentMode) => void
  /**
   * 发送键偏好（通用设置里可改）。
   * `enter` = 回车发送（默认）；`mod-enter` = 回车换行、Ctrl/⌘+Enter 才发送。
   */
  sendWith?: 'enter' | 'mod-enter'
}

const MAX_HEIGHT = 220

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  streaming,
  disabled = false,
  modes,
  current,
  onModeChange,
  sendWith = 'enter',
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  // 输入法组合状态：true 表示用户正在用拼音/日文等输入法选词
  const composingRef = useRef(false)

  // 自适应高度：先归零再按 scrollHeight 设，否则高度只会越来越大
  useEffect(() => {
    const element = textareaRef.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, MAX_HEIGHT)}px`
  }, [value])

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== 'Enter') return
    // 输入法组合中的回车是"选词"，不是"发送"
    if (composingRef.current || event.nativeEvent.isComposing) return

    // 【为什么 Ctrl/Cmd+Enter 在两种偏好下都发送】
    // 它是"我确定要发"的强信号，不存在误触可能。用户从默认切到
    // "Ctrl+Enter 才发送"之后，肌肉记忆还会按一会儿 Ctrl+Enter ——
    // 此时不发送只会让人以为键盘坏了。
    const modified = event.ctrlKey || event.metaKey
    if (modified) {
      event.preventDefault()
      if (!streaming && !disabled) onSend()
      return
    }

    if (event.shiftKey) return // Shift+Enter 一律是换行
    if (sendWith === 'mod-enter') return // 该偏好下裸回车就是换行

    event.preventDefault()
    if (!streaming && !disabled) onSend()
  }

  return (
    <div className="composer" data-disabled={disabled}>
      {current !== 'react' && (
        <p className="composer__hint" role="note">独立任务：本轮不使用会话历史，请在消息中提供所需背景。</p>
      )}
      <textarea
        ref={textareaRef}
        className="composer__input"
        value={value}
        rows={1}
        placeholder={
          sendWith === 'enter'
            ? '问我任何问题…（Enter 发送，Shift+Enter 换行）'
            : '问我任何问题…（Ctrl/⌘+Enter 发送，Enter 换行）'
        }
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        onCompositionStart={() => {
          composingRef.current = true
        }}
        onCompositionEnd={() => {
          composingRef.current = false
        }}
        spellCheck={false}
      />

      <div className="composer__bar">
        <span className="composer__hint">
          {/* 提示跟着偏好走：这里的按键说明与"设置 → 通用 → 发送键"必须一致，
              否则用户会照着一个不成立的说明去按键。 */}
          <span className="composer__keys">
            {sendWith === 'enter' ? (
              <>
                <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行
              </>
            ) : (
              <>
                <kbd>Ctrl</kbd>+<kbd>Enter</kbd> 发送 · <kbd>Enter</kbd> 换行
              </>
            )}
          </span>
          {value.length > 0 ? ` · ${value.length} 字` : ''}
        </span>

        {/* 形态选择器。放在输入区而不是顶栏：它是**每次发送时**的一个选择，
            与"当前会话"同级，而不是全局设置。 */}
        {modes.length > 1 ? (
          <div className="modes" role="group" aria-label="Agent 形态">
            {modes.map((mode) => {
              const meta = AGENT_MODE_META[mode]
              return (
                <button
                  key={mode}
                  type="button"
                  className={`modes__item${mode === current ? ' modes__item--active' : ''}`}
                  onClick={() => onModeChange(mode)}
                  disabled={streaming}
                  title={meta?.hint ?? mode}
                  aria-pressed={mode === current}
                >
                  {meta?.label ?? mode}
                </button>
              )
            })}
          </div>
        ) : null}

        {streaming ? (
          <button type="button" className="btn composer__stop" onClick={onStop}>
            <IconStop size={11} />
            停止生成
          </button>
        ) : (
          <button
            type="button"
            className="btn btn--primary composer__send"
            onClick={onSend}
            disabled={disabled || value.trim() === ''}
          >
            <IconArrowUp size={13} />
            发送
          </button>
        )}
      </div>
    </div>
  )
}
