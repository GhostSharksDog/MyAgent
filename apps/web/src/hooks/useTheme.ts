/** 首屏沿用 head 脚本的显示值；偏好保留 light / dark / system 三态。 */
import { useCallback, useEffect, useState } from 'react'
import {
  initialResolvedTheme,
  nextThemePreference,
  readThemePreference,
  resolveTheme,
  THEME_STORAGE_KEY,
} from '../lib/theme'
import type { ResolvedTheme, ThemePreference } from '../lib/theme'

export type { ResolvedTheme, ThemePreference } from '../lib/theme'

const SYSTEM_QUERY = '(prefers-color-scheme: dark)'

function readPreference(): ThemePreference {
  return readThemePreference(() => localStorage.getItem(THEME_STORAGE_KEY))
}

function systemDark(): boolean {
  try {
    return typeof window !== 'undefined' && Boolean(window.matchMedia?.(SYSTEM_QUERY).matches)
  } catch {
    return false
  }
}

function applyTheme(theme: ResolvedTheme): void {
  if (typeof document !== 'undefined') document.documentElement.dataset['theme'] = theme
}

export interface UseThemeResult {
  preference: ThemePreference
  theme: ResolvedTheme
  setPreference: (next: ThemePreference) => void
  /** 在 明亮 → 暗色 → 跟随系统 之间循环。 */
  cycle: () => void
}

export function useTheme(): UseThemeResult {
  const [preference, setPreferenceState] = useState<ThemePreference>(readPreference)
  const [theme, setTheme] = useState<ResolvedTheme>(() =>
    initialResolvedTheme(
      typeof document === 'undefined' ? undefined : document.documentElement.dataset['theme'],
      preference,
      systemDark(),
    ),
  )

  // 偏好变化 → 落盘 + 立刻应用
  useEffect(() => {
    const next = resolveTheme(preference, systemDark())
    setTheme(next)
    applyTheme(next)
    try {
      localStorage.setItem(THEME_STORAGE_KEY, preference)
    } catch {
      /* 存储失败时当前选择仍生效；下次访问按浅色默认值初始化。 */
    }
  }, [preference])

  // 跟随系统时，监听系统的切换事件
  useEffect(() => {
    if (preference !== 'system' || typeof window === 'undefined' || !window.matchMedia) {
      return undefined
    }
    let query: MediaQueryList
    try {
      query = window.matchMedia(SYSTEM_QUERY)
    } catch {
      return undefined
    }
    const onChange = (event: MediaQueryListEvent): void => {
      const next = resolveTheme('system', event.matches)
      setTheme(next)
      applyTheme(next)
    }
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [preference])

  const cycle = useCallback(() => setPreferenceState(nextThemePreference), [])
  return { preference, theme, setPreference: setPreferenceState, cycle }
}
