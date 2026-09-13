/**
 * 样式系统的完整性自测。
 *
 * ============================================================
 * 为什么样式也需要测试
 * ============================================================
 * CSS 的错误几乎全是**静默的**：写错一个变量名（`var(--txt-2)`），
 * 浏览器不会报错，那个属性直接失效，视觉上表现为"某处颜色怪怪的"，
 * 只有在恰好看到那个界面时才会发现。手写 CSS 不用框架时，
 * 这类错误是最大的一类回归来源。
 *
 * 这里用三条不依赖浏览器、不依赖任何依赖的规则把它拦住：
 *
 *   1. **变量必须已定义**：所有出现的 `var(--x)` 都能在样式表里找到 `--x:`。
 *   2. **颜色不许写死在组件里**：除 tokens.css 之外不得出现十六进制颜色。
 *      这条规则保证"换主题只改一处"这个承诺是真的 —— 一旦有人图省事
 *      在组件里写 `#fff`，浅色主题下就会出现读不清的文字。
 *   3. **样式入口必须完整**：src/styles/index.css 里的 @import 与实际文件一致，
 *      避免新增的样式文件忘了被引入（本地看着正常，因为别的文件碰巧定义了同名类）。
 *
 * 顺带还检查每个文件的括号是否配对 —— CSS 解析器遇到未闭合的块会
 * 把后面的规则整段吞掉，这也是典型的"看起来没生效"。
 */

import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const STYLE_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'styles')
const TOKENS_FILE = 'tokens.css'
const ENTRY_FILE = 'index.css'

function files() {
  return readdirSync(STYLE_DIR).filter((name) => name.endsWith('.css'))
}

function read(name) {
  return readFileSync(join(STYLE_DIR, name), 'utf8')
}

/** 去掉注释与字符串内容，避免注释里的示例值干扰检查。 */
function stripNoise(css) {
  return css.replace(/\/\*[\s\S]*?\*\//g, '')
}

/** 逐行扫描，返回出现 `var(--x)` 但文件里找不到定义的位置。 */
function undefinedVariables(name, css) {
  const clean = stripNoise(css)
  const defined = new Set()
  for (const match of clean.matchAll(/(^|[\s;{])(--[a-zA-Z0-9-]+)\s*:/g)) {
    defined.add(match[2])
  }
  // 定义可能分散在多个文件（例如 tokens.css + 主题覆盖），所以先收集全局定义
  for (const other of files()) {
    const otherClean = stripNoise(read(other))
    for (const match of otherClean.matchAll(/(^|[\s;{])(--[a-zA-Z0-9-]+)\s*:/g)) {
      defined.add(match[2])
    }
  }

  const missing = []
  const lines = clean.split('\n')
  lines.forEach((line, index) => {
    for (const match of line.matchAll(/var\(\s*(--[a-zA-Z0-9-]+)/g)) {
      if (!defined.has(match[1])) {
        missing.push(`${name}:${index + 1} 使用了未定义的变量 ${match[1]}`)
      }
    }
  })
  return missing
}

test('每个样式文件的花括号都配对', () => {
  for (const name of files()) {
    const clean = stripNoise(read(name))
    const open = (clean.match(/{/g) ?? []).length
    const close = (clean.match(/}/g) ?? []).length
    assert.equal(open, close, `${name} 的花括号不配对（{ ${open} 个，} ${close} 个）`)
  }
})

test('所有 var(--x) 引用的变量都真的有定义', () => {
  const missing = files().flatMap((name) => undefinedVariables(name, read(name)))
  assert.deepEqual(missing, [], `存在未定义的 CSS 变量：\n${missing.join('\n')}`)
})

test('颜色只允许定义在 tokens.css 里（组件不得写死颜色）', () => {
  const offenders = []
  for (const name of files()) {
    if (name === TOKENS_FILE) continue
    const clean = stripNoise(read(name))
    clean.split('\n').forEach((line, index) => {
      const hex = line.match(/#[0-9a-fA-F]{3,8}\b/)
      if (hex) offenders.push(`${name}:${index + 1} 写死了颜色 ${hex[0]} → 应该新增一个 token`)
    })
  }
  assert.deepEqual(offenders, [], `组件样式里出现了硬编码颜色：\n${offenders.join('\n')}`)
})

test('样式入口 index.css 引用的文件都存在，且没有遗漏任何样式文件', () => {
  const entry = read(ENTRY_FILE)
  const imported = [...entry.matchAll(/@import\s+'\.\/([^']+)'/g)].map((match) => match[1])

  for (const name of imported) {
    assert.ok(files().includes(name), `index.css 引用了不存在的 ${name}`)
  }

  const missing = files().filter((name) => name !== ENTRY_FILE && !imported.includes(name))
  assert.deepEqual(missing, [], `这些样式文件没有被 index.css 引入：${missing.join('、')}`)
})

test('浅色主题覆盖了所有需要覆盖的语义色', () => {
  const tokens = stripNoise(read(TOKENS_FILE))
  const lightBlock = tokens.slice(tokens.indexOf("[data-theme='light']"))
  assert.ok(lightBlock.length > 0, '找不到浅色主题区块')

  // 这几个 token 决定"文字是否读得清"，浅色下必须重新取值，
  // 漏掉任何一个都会出现"深色文字配深色底"的低对比问题。
  for (const token of ['--bg', '--bg-panel', '--surface', '--border', '--text', '--text-2', '--text-3', '--accent']) {
    assert.ok(
      new RegExp(`${token}\\s*:`).test(lightBlock),
      `浅色主题没有重新定义 ${token}`,
    )
  }
})
