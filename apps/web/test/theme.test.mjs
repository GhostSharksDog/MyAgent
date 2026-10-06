import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import {
  initialResolvedTheme,
  nextThemePreference,
  parseThemePreference,
  readThemePreference,
  resolveTheme,
  THEME_STORAGE_KEY,
} from '../src/lib/theme.ts'

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
const script = html.match(/<script id="theme-init">([\s\S]*?)<\/script>/)?.[1]
assert.ok(script, '必须执行页面实际使用的首屏初始化脚本')

function initialize(stored, systemDark, { storageError = false, mediaAvailable = true } = {}) {
  const document = { documentElement: { dataset: { theme: 'light' } } }
  const keys = []
  const localStorage = {
    getItem(key) {
      keys.push(key)
      if (storageError) throw new Error('storage unavailable')
      return stored
    },
  }
  const window = mediaAvailable ? {
    matchMedia(query) {
      assert.equal(query, '(prefers-color-scheme: dark)')
      return { matches: systemDark }
    },
  } : {}
  vm.runInNewContext(script, { document, localStorage, window })
  assert.deepEqual(keys, [THEME_STORAGE_KEY])
  return document.documentElement.dataset.theme
}

test('首次访问采用浅色，即使系统当前是深色', () => {
  assert.equal(parseThemePreference(null), 'light')
  assert.equal(initialize(null, true), 'light')
  assert.equal(initialize(null, false), 'light')
  assert.match(html, /<html[^>]+data-theme="light"/)
  assert.match(html, /<title>Legacy · AI 助手<\/title>/)
})

for (const stored of ['light', 'dark', 'system', null, '', 'Dark', 'unexpected']) {
  for (const systemDark of [true, false]) {
    test(`head 脚本与 Hook 纯逻辑一致：${String(stored)} / systemDark=${systemDark}`, () => {
      const preference = readThemePreference(() => stored)
      const resolved = resolveTheme(preference, systemDark)
      assert.equal(initialize(stored, systemDark), resolved)
      if (stored === 'light' || stored === 'dark' || stored === 'system') {
        assert.equal(preference, stored, '旧的合法主题偏好不能丢失')
      } else {
        assert.equal(preference, 'light')
      }
    })
  }
}

test('读取存储异常采用浅色，初始化脚本也不抛错', () => {
  assert.equal(readThemePreference(() => { throw new Error('denied') }), 'light')
  assert.equal(initialize('dark', true, { storageError: true }), 'light')
})

test('系统媒体查询不可用时，system 回退浅色', () => {
  assert.equal(resolveTheme('system'), 'light')
  assert.equal(initialize('system', true, { mediaAvailable: false }), 'light')
})

test('Hook 初次绘制沿用有效 DOM 值，非法或缺失值使用偏好解析', () => {
  assert.equal(initialResolvedTheme('dark', 'light'), 'dark')
  assert.equal(initialResolvedTheme('light', 'dark'), 'light')
  assert.equal(initialResolvedTheme('bad', 'system', true), 'dark')
  assert.equal(initialResolvedTheme(undefined, 'light', true), 'light')
})

test('主题依次循环 light → dark → system → light', () => {
  assert.equal(nextThemePreference('light'), 'dark')
  assert.equal(nextThemePreference('dark'), 'system')
  assert.equal(nextThemePreference('system'), 'light')
})
