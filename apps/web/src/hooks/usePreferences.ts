/**
 * 界面偏好（纯前端，localStorage）。
 *
 * ============================================================
 * 与"设置"的区别：这些不是配置，是偏好
 * ============================================================
 * `.env` 里的东西是**服务端配置**（换台机器要重配、影响所有客户端）；
 * 而"发送用 Enter 还是 Ctrl+Enter""要不要自动滚到底"是**这台设备上这个人的习惯**，
 * 它不该写进 `.env` —— 那会让"我改了自己的习惯"变成"改了所有人的"。
 *
 * 所以这个 Hook 只落 localStorage，与主题（`useTheme`）同一层级。
 * 主题放在独立的 Hook 里是因为它还要处理"跟随系统"的变化监听；
 * 其余偏好没有这类副作用，统一放在这里。
 *
 * ============================================================
 * 每个偏好都必须**真的被用上**
 * ============================================================
 * 加一个"点了没反应"的开关，比不加更糟：用户会以为功能坏了，
 * 而更常见的结果是他不再相信这个设置页里的任何东西。
 * 所以每加一项，都要在界面上找得到它的效果（见下方 defaultPreferences 的注释）。
 */

import { useCallback, useEffect, useState } from 'react'

const STORAGE_KEY = 'legacy.prefs'

export interface Preferences {
  /** 发送键：Enter 直接发送（默认），或 Ctrl/Cmd+Enter 才发送（写长文时更稳） */
  sendWith: 'enter' | 'mod-enter'
  /** 新消息到达时是否自动滚到底 */
  autoScroll: boolean
  /** 是否显示每条回答下方的耗时/token 元信息 */
  showMeta: boolean
}

export const defaultPreferences: Preferences = {
  sendWith: 'enter',
  autoScroll: true,
  showMeta: true,
}

function read(): Preferences {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return defaultPreferences
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return defaultPreferences
    const obj = parsed as Partial<Preferences>
    // 逐字段收敛：storage 里可能是旧版本留下的值，一个坏字段
    // 不该让整份偏好失效（那会让用户的所有设置突然回到默认）
    return {
      sendWith: obj.sendWith === 'mod-enter' ? 'mod-enter' : 'enter',
      autoScroll: obj.autoScroll !== false,
      showMeta: obj.showMeta !== false,
    }
  } catch {
    return defaultPreferences
  }
}

export interface UsePreferencesResult {
  prefs: Preferences
  set: <K extends keyof Preferences>(key: K, value: Preferences[K]) => void
  reset: () => void
}

export function usePreferences(): UsePreferencesResult {
  const [prefs, setPrefs] = useState<Preferences>(read)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs))
    } catch {
      /* 隐私模式下写不了：偏好不是关键路径，忽略 */
    }
  }, [prefs])

  const set = useCallback(<K extends keyof Preferences>(key: K, value: Preferences[K]) => {
    setPrefs((current) => ({ ...current, [key]: value }))
  }, [])

  const reset = useCallback(() => setPrefs(defaultPreferences), [])

  return { prefs, set, reset }
}
