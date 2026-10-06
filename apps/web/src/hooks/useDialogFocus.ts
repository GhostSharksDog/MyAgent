import { useLayoutEffect, useRef } from 'react'
import type { RefObject } from 'react'

interface DialogFocusOptions {
  open: boolean
  containerRef: RefObject<HTMLElement | null>
  onClose: () => void
  initialFocusRef?: RefObject<HTMLElement | null>
  /** 并列文件面板不抢焦点、不限制 Tab；窄屏抽屉与弹窗才是模态。 */
  modal?: boolean
}

interface FocusScope {
  root: HTMLElement
  isModal: () => boolean
}

// 工作区面板可以在设置弹窗下面保持打开。只有最上层模态处理 Esc/Tab。
const scopes: FocusScope[] = []
const FOCUSABLE = 'a[href], button, summary, input, select, textarea, [tabindex], [contenteditable="true"]'

function focusableElements(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((element) =>
    element.tabIndex >= 0 &&
    !element.matches(':disabled') &&
    !element.closest('[inert]') &&
    element.getClientRects().length > 0,
  )
}

export function useDialogFocus({
  open,
  containerRef,
  onClose,
  initialFocusRef,
  modal = true,
}: DialogFocusOptions): void {
  // 父组件的就地回调会在流式渲染时变化，不能因此重复恢复或抢走焦点。
  const options = useRef({ onClose, initialFocusRef, modal })
  options.current = { onClose, initialFocusRef, modal }

  useLayoutEffect(() => {
    if (!open) return
    const root = containerRef.current
    if (!root) return
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const scope: FocusScope = { root, isModal: () => options.current.modal }
    scopes.push(scope)

    const onKey = (event: KeyboardEvent): void => {
      if (event.defaultPrevented) return
      const active = document.activeElement
      const modalScopes = scopes.filter((item) => item.isModal())
      const owner = modalScopes.at(-1) ?? [...scopes].reverse().find((item) => item.root.contains(active))
      if (owner !== scope) return

      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        options.current.onClose()
        return
      }
      if (event.key !== 'Tab' || !options.current.modal) return
      const targets = focusableElements(root)
      const first = targets[0]
      const last = targets.at(-1)
      if (!first || !last) {
        event.preventDefault()
        root.focus()
      } else if (!root.contains(active) || active === root ||
                 (event.shiftKey ? active === first : active === last)) {
        event.preventDefault()
        const target = event.shiftKey ? last : first
        target.focus()
      }
    }

    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      const index = scopes.indexOf(scope)
      if (index !== -1) scopes.splice(index, 1)
      const active = document.activeElement
      // 用户已把焦点移往别的区域时不夺回；被移除的控件通常退回 body。
      if (previous?.isConnected && !previous.closest('[inert]') && previous.getClientRects().length > 0 &&
          (root.contains(active) || active === document.body || active === null)) {
        previous.focus()
      }
    }
  }, [open, containerRef])

  useLayoutEffect(() => {
    if (!open || !modal) return
    const root = containerRef.current
    if (!root || root.contains(document.activeElement)) return
    const requested = options.current.initialFocusRef?.current
    const target = requested && root.contains(requested) ? requested : focusableElements(root)[0]
    const initial = target ?? root
    initial.focus()
  }, [open, modal, containerRef])
}
