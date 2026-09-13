/**
 * Markdown 解析器自测。
 *
 * 自研渲染器最大的风险是"在某些输入上渲染错了，但平时看不出来"。
 * 这里针对模型输出的典型形态做断言：标题紧跟正文（不留空行）、
 * 表格、围栏代码块、嵌套列表，以及两类**必须不误判**的情况：
 *
 *   - `2 * 3 * 4` 不能被渲染成斜体；
 *   - `[点我](javascript:alert(1))` 不能变成可点击的链接（XSS 向量）。
 *
 * 注意断言的是**解析结果（AST）**，不是渲染出的 HTML ——
 * 这样测试不依赖 React，也不需要 DOM。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { parseBlocks, parseInline } from '../src/lib/markdown.ts'

/** 把行内节点压成便于断言的字符串形式。 */
function flat(nodes) {
  return nodes
    .map((node) => {
      switch (node.t) {
        case 'text':
          return node.v
        case 'code':
          return `\`${node.v}\``
        case 'strong':
          return `**${flat(node.children)}**`
        case 'em':
          return `*${flat(node.children)}*`
        case 'del':
          return `~~${flat(node.children)}~~`
        case 'link':
          return `[${flat(node.children)}](${node.href})`
        default:
          return '?'
      }
    })
    .join('')
}

test('行内：代码、粗体、斜体、删除线、链接', () => {
  assert.equal(flat(parseInline('用 `pnpm build` 构建')), '用 `pnpm build` 构建')
  assert.equal(flat(parseInline('**重点**内容')), '**重点**内容')
  assert.equal(flat(parseInline('这是 _斜体_ 与 *强调*')), '这是 *斜体* 与 *强调*')
  assert.equal(flat(parseInline('~~删掉~~')), '~~删掉~~')
  assert.equal(
    flat(parseInline('见 [文档](https://example.com/a?b=1)')),
    '见 [文档](https://example.com/a?b=1)',
  )
})

test('行内：裸 URL 自动成链接', () => {
  assert.equal(flat(parseInline('参考 https://fastapi.tiangolo.com/docs 即可')), '参考 [https://fastapi.tiangolo.com/docs](https://fastapi.tiangolo.com/docs) 即可')
})

test('行内：算术里的星号不会被误判为斜体', () => {
  assert.equal(flat(parseInline('半径 2 * 3 * 4 的结果')), '半径 2 * 3 * 4 的结果')
})

test('行内：危险的 javascript: 链接被拒绝，降级为纯文本', () => {
  assert.equal(flat(parseInline('[点我](javascript:alert(1))')), '[点我](javascript:alert(1))')
})

test('行内：反斜杠转义使标记原样显示', () => {
  assert.equal(flat(parseInline('\\*不是斜体\\*')), '*不是斜体*')
})

test('行内：未闭合的标记降级为普通文本，不吞掉内容', () => {
  assert.equal(flat(parseInline('**没闭合的粗体')), '**没闭合的粗体')
  assert.equal(flat(parseInline('`没闭合的行内代码')), '`没闭合的行内代码')
})

test('块级：标题与段落（标题后无空行也要正确断开）', () => {
  const blocks = parseBlocks('# 匹配度分析\n这是正文，紧跟标题。\n\n## 差距清单\n- 缺少分布式经验')
  assert.equal(blocks[0].t, 'heading')
  assert.equal(blocks[0].level, 1)
  assert.equal(flat(blocks[0].content), '匹配度分析')

  assert.equal(blocks[1].t, 'paragraph', '宽松解析：标题下一行就是正文')
  assert.equal(flat(blocks[1].content), '这是正文，紧跟标题。')

  assert.equal(blocks[2].t, 'heading')
  assert.equal(blocks[2].level, 2)
  assert.equal(blocks[3].t, 'list')
})

test('块级：围栏代码块完整保留（含空行与缩进）', () => {
  const source = ['```python', 'def f(x):', '', '    return x * 2', '```'].join('\n')
  const blocks = parseBlocks(source)
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0].t, 'code')
  assert.equal(blocks[0].lang, 'python')
  assert.equal(blocks[0].value, 'def f(x):\n\n    return x * 2')
})

test('块级：未闭合的代码块不会吞掉后面的内容之外的东西（容错到结尾）', () => {
  const blocks = parseBlocks('```\ncode only\n')
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0].t, 'code')
  assert.equal(blocks[0].value, 'code only')
})

test('块级：表格（含对齐与列数不等时的补齐）', () => {
  const source = [
    '| 维度 | 得分 | 说明 |',
    '| :--- | ---: | :---: |',
    '| 技能匹配 | 85 | 基本吻合 |',
    '| 项目深度 | 70 |',
  ].join('\n')

  const blocks = parseBlocks(source)
  assert.equal(blocks.length, 1)
  const table = blocks[0]
  assert.equal(table.t, 'table')
  assert.deepEqual(table.align, ['left', 'right', 'center'])
  assert.equal(flat(table.head[0]), '维度')
  assert.equal(table.rows.length, 2)
  assert.equal(table.rows[1].length, 3, '列数不足时补空单元格，避免渲染出缺列的行')
  assert.equal(flat(table.rows[1][2]), '')
})

test('块级：表格里的转义竖线不会被当成列分隔', () => {
  const blocks = parseBlocks('| a | b |\n| --- | --- |\n| x \\| y | z |')
  assert.equal(blocks[0].rows[0].length, 2)
  assert.equal(flat(blocks[0].rows[0][0]), 'x | y')
})

test('块级：有序与无序列表，含一层嵌套', () => {
  const blocks = parseBlocks('- 第一点\n- 第二点\n  - 嵌套一\n  - 嵌套二\n1. 有序一\n2. 有序二')
  assert.equal(blocks[0].t, 'list')
  assert.equal(blocks[0].ordered, false)
  assert.equal(blocks[0].items.length, 2)
  assert.equal(blocks[0].items[1].children.length, 1)
  assert.equal(blocks[0].items[1].children[0].t, 'list')
  assert.equal(blocks[0].items[1].children[0].items.length, 2)

  assert.equal(blocks[1].t, 'list')
  assert.equal(blocks[1].ordered, true)
  assert.equal(blocks[1].start, 1)
})

test('块级：引用、分割线', () => {
  const blocks = parseBlocks('> 引用第一行\n> 引用第二行\n\n---\n')
  assert.equal(blocks[0].t, 'quote')
  assert.equal(blocks[0].children[0].t, 'paragraph')
  assert.equal(flat(blocks[0].children[0].content), '引用第一行 引用第二行')
  assert.equal(blocks[1].t, 'hr')
})

test('块级：CRLF 换行也能解析（模型偶发输出 \\r\\n）', () => {
  const blocks = parseBlocks('# 标题\r\n正文\r\n')
  assert.equal(blocks[0].t, 'heading')
  assert.equal(flat(blocks[0].content), '标题')
  assert.equal(flat(blocks[1].content), '正文')
})

test('块级：段落里的换行按同一段处理，不产生断裂的碎段落', () => {
  const blocks = parseBlocks('第一行\n第二行\n第三行')
  assert.equal(blocks.length, 1)
  assert.equal(flat(blocks[0].content), '第一行 第二行 第三行')
})
