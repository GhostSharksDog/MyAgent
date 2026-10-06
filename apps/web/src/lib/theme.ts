/** 主题偏好与显示值分开：system 是持久化偏好，light/dark 是实际显示值。 */
export type ThemePreference = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'legacy.theme'

/** 未设置、旧脏值与不可用存储都采用浅色，首次访问不受系统主题影响。 */
export function parseThemePreference(stored: unknown): ThemePreference {
  return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'light'
}

export function readThemePreference(readStored: () => unknown): ThemePreference {
  try {
    return parseThemePreference(readStored())
  } catch {
    return 'light'
  }
}

export function resolveTheme(preference: ThemePreference, systemDark = false): ResolvedTheme {
  return preference === 'system' ? (systemDark ? 'dark' : 'light') : preference
}

/** 沿用 head 同步脚本已绘制的值；DOM 缺失或异常时才重新解析。 */
export function initialResolvedTheme(
  documentTheme: unknown,
  preference: ThemePreference,
  systemDark = false,
): ResolvedTheme {
  return documentTheme === 'light' || documentTheme === 'dark'
    ? documentTheme
    : resolveTheme(preference, systemDark)
}

export function nextThemePreference(current: ThemePreference): ThemePreference {
  return current === 'light' ? 'dark' : current === 'dark' ? 'system' : 'light'
}
