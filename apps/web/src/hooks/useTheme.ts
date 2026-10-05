/**
 * 主题 Hook：system / light / dark 三态。
 *
 * ============================================================
 * 为什么是"偏好"而不是"当前值"
 * ============================================================
 * 只存一个布尔（是不是暗色）会丢掉一个重要信息：用户**有没有表过态**。
 * 存偏好（system/light/dark）之后，"跟随系统"仍然是一个稳定选项 ——
 * 用户白天把系统切到浅色，页面会自动跟着变，不需要重新点一次切换。
 *
 * ============================================================
 * 为什么初始值从 DOM 上读
 * ============================================================
 * `index.html` 里的同步脚本已经在首次绘制前把 `data-theme` 写到了 <html> 上
 * （避免白底闪烁）。这里读回它作为 `resolved` 的初始值，
 * 保证 React 首帧与已渲染的页面**一致** —— 否则会看到主题"跳"一下。
 */

import { useCallback, useEffect, useState } from 'react'

export type ThemePreference = 'system' | 'light' | 'dark'
export type ResolvedTheme = 'light' | 'dark'

const STORAGE_KEY = 'legacy.theme'

function readPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') return stored
  } catch {
    /* 隐私模式下 localStorage 不可用，退回默认值 */
  }
  return 'system'
}

function resolve(preference: ThemePreference): ResolvedTheme {
  if (preference !== 'system') return preference
  if (typeof window !== 'undefined' && window.matchMedia) {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
  }
  return 'dark'
}

function applyTheme(theme: ResolvedTheme): void {
  document.documentElement.dataset['theme'] = theme
}

export interface UseThemeResult {
  preference: ThemePreference
  theme: ResolvedTheme
  setPreference: (next: ThemePreference) => void
  /** 在 明亮 → 暗色 → 跟随系统 之间循环。 */
  cycle: () => void
}

const ORDER: ThemePreference[] = ['dark', 'light', 'system']

export function useTheme(): UseThemeResult {
  const [preference, setPreferenceState] = useState<ThemePreference>(readPreference)
  const [theme, setTheme] = useState<ResolvedTheme>(() =>
    typeof document === 'undefined'
      ? 'dark'
      : (document.documentElement.dataset['theme'] as ResolvedTheme | undefined) ??
        resolve(readPreference()),
  )

  // 偏好变化 → 落盘 + 立刻应用
  useEffect(() => {
    const next = resolve(preference)
    setTheme(next)
    applyTheme(next)
    try {
      localStorage.setItem(STORAGE_KEY, preference)
    } catch {
      /* 忽略存储失败：主题不是关键路径 */
    }
  }, [preference])

  // 跟随系统时，监听系统的切换事件
  useEffect(() => {
    if (preference !== 'system' || !window.matchMedia) return undefined
    const query = window.matchMedia('(prefers-color-scheme: light)')
    const onChange = (): void => {
      const next = resolve('system')
      setTheme(next)
      applyTheme(next)
    }
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [preference])

  const cycle = useCallback(() => {
    setPreferenceState((current) => {
      const index = ORDER.indexOf(current)
      return ORDER[(index + 1) % ORDER.length] ?? 'dark'
    })
  }, [])

  return { preference, theme, setPreference: setPreferenceState, cycle }
}
