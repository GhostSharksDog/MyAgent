/**
 * 通用设置：外观与界面偏好。
 *
 * ============================================================
 * 这里**没有"语言"**
 * ============================================================
 * 全应用的文案目前都是中文，而做成可切换需要把 20+ 个组件里的几百条中文
 * 抽成字典（一份独立的工作）。放一个点了没反应的下拉框比不放更糟：
 * 用户会以为功能坏了，之后不再相信这个设置页里的任何东西。
 * 所以宁可不放 —— 需求确认时也是这么定的。
 */

import type { Preferences } from '../../hooks/usePreferences'
import type { ThemePreference } from '../../hooks/useTheme'
import { IconCheck } from '../Icons'

const THEMES: { value: ThemePreference; label: string; note: string }[] = [
  { value: 'light', label: '浅色', note: '白天看得清' },
  { value: 'dark', label: '深色', note: '夜里不刺眼' },
  { value: 'system', label: '跟随系统', note: '系统切换时自动跟着变' },
]

export interface GeneralSectionProps {
  themePreference: ThemePreference
  onThemeChange: (value: ThemePreference) => void
  prefs: Preferences
  onPrefChange: <K extends keyof Preferences>(key: K, value: Preferences[K]) => void
  onResetPrefs: () => void
}

export function GeneralSection({
  themePreference,
  onThemeChange,
  prefs,
  onPrefChange,
  onResetPrefs,
}: GeneralSectionProps) {
  return (
    <>
      <section className="settings__group">
        <h3 className="settings__legend">外观</h3>
        <p className="settings__hint settings__hint--block">
          主题保存在这台设备上，不影响服务端配置。
        </p>
        <div className="theme-cards">
          {THEMES.map((item) => {
            const active = themePreference === item.value
            return (
              <button
                key={item.value}
                type="button"
                className={`theme-card${active ? ' theme-card--active' : ''}`}
                onClick={() => onThemeChange(item.value)}
                aria-pressed={active}
              >
                <span className={`theme-card__preview theme-card__preview--${item.value}`} aria-hidden>
                  <span className="theme-card__bar" />
                  <span className="theme-card__block" />
                </span>
                <span className="theme-card__label">
                  {item.label}
                  {active && <IconCheck size={14} />}
                </span>
                <span className="theme-card__note">{item.note}</span>
              </button>
            )
          })}
        </div>
      </section>

      <section className="settings__group">
        <h3 className="settings__legend">对话</h3>

        <div className="settings__field">
          <span className="settings__label">发送键</span>
          <div className="segmented" role="group" aria-label="发送键">
            <button
              type="button"
              className={`segmented__item${prefs.sendWith === 'enter' ? ' segmented__item--on' : ''}`}
              onClick={() => onPrefChange('sendWith', 'enter')}
              aria-pressed={prefs.sendWith === 'enter'}
            >
              Enter 发送
            </button>
            <button
              type="button"
              className={`segmented__item${prefs.sendWith === 'mod-enter' ? ' segmented__item--on' : ''}`}
              onClick={() => onPrefChange('sendWith', 'mod-enter')}
              aria-pressed={prefs.sendWith === 'mod-enter'}
            >
              Ctrl/⌘ + Enter 发送
            </button>
          </div>
          <span className="settings__hint">
            {prefs.sendWith === 'enter'
              ? '回车即发送。要换行用 Shift + Enter。'
              : '回车换行，Ctrl/⌘ + Enter 才发送 —— 写长问题时不容易误发。'}
          </span>
        </div>

        <label className="settings__field settings__field--check">
          <input
            type="checkbox"
            checked={prefs.autoScroll}
            onChange={(e) => onPrefChange('autoScroll', e.target.checked)}
          />
          <span>
            回答变长时自动滚到底
            <span className="settings__hint">关掉后可以自己往上翻，不会被拽回去。</span>
          </span>
        </label>

        <label className="settings__field settings__field--check">
          <input
            type="checkbox"
            checked={prefs.showMeta}
            onChange={(e) => onPrefChange('showMeta', e.target.checked)}
          />
          <span>
            显示耗时与 token
            <span className="settings__hint">每条回答下方的元信息；关掉界面更干净。</span>
          </span>
        </label>
      </section>

      <section className="settings__group">
        <h3 className="settings__legend">恢复</h3>
        <div className="settings__actions">
          <button type="button" className="btn btn--ghost" onClick={onResetPrefs}>
            恢复界面默认值
          </button>
          <span className="settings__hint">只重置外观与上面的交互偏好，不动模型与知识库配置。</span>
        </div>
      </section>
    </>
  )
}
