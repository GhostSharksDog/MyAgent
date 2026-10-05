/**
 * "黏底"滚动 Hook。
 *
 * 聊天界面里滚动到底部看起来是件小事，但做错了体验会很差：
 *
 *   - 只在"用户本来就在底部"时才自动跟随。用户往上翻看历史时
 *     还在每来一个 token 就把他拽回底部，是聊天界面的经典恶习。
 *   - 用 `scrollTop = scrollHeight`（瞬时）而不是 `scrollIntoView({behavior:'smooth'})`：
 *     流式输出每秒触发几十次平滑滚动，浏览器会把动画排成队列，
 *     结果就是又卡又飘。瞬时跳转在流式场景下反而更"顺"。
 *   - 判定阈值给 80px：太灵敏（0px）会让"差一点点到底"也算离开底部，
 *     之后再也不跟随，用户会以为程序卡死了。
 */

import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'

const STICK_THRESHOLD = 80

export function useStickToBottom<T extends HTMLElement>(
  ref: RefObject<T | null>,
  dependency: unknown,
  /**
   * 是否允许自动跟随（通用设置里的开关）。
   *
   * 关掉之后行为是"什么都不做"，而不是"滚到别处"：用户想自己翻看时，
   * 任何自动滚动都是干扰。注意它与下面的 `stickRef` 是**两层**判断 ——
   * 那一层管"用户是不是翻上去了"，这一层管"用户根本不想被跟随"。
   */
  enabled = true,
): void {
  const stickRef = useRef(true)

  useEffect(() => {
    const element = ref.current
    if (!element) return undefined

    const onScroll = (): void => {
      const distance = element.scrollHeight - element.scrollTop - element.clientHeight
      stickRef.current = distance < STICK_THRESHOLD
    }

    element.addEventListener('scroll', onScroll, { passive: true })
    return () => element.removeEventListener('scroll', onScroll)
  }, [ref])

  useEffect(() => {
    const element = ref.current
    if (!element || !enabled || !stickRef.current) return
    element.scrollTop = element.scrollHeight
  }, [ref, dependency, enabled])
}
