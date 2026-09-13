/**
 * 手写的轻量 Markdown 解析器（**刻意不引入 react-markdown / marked**）。
 *
 * ============================================================
 * 为什么自己写
 * ============================================================
 *
 * 1. **依赖成本**。react-markdown 会带进 remark / rehype / micromark 一整棵树
 *    （几十个包），而我们真正需要的只是"标题 / 粗体 / 列表 / 表格 / 代码块 / 行内代码"
 *    这六样。为一个聊天框装一整套 CommonMark 实现，收益与体积不成比例。
 * 2. **可控**。默认的 Markdown 渲染会生成原始 HTML（历史上 `dangerouslySetInnerHTML`
 *    是 XSS 的常客）。这里输出的是 **React 元素树**，全程不碰 innerHTML ——
 *    模型输出里的 `<script>` 只会被当成普通文本显示。安全边界比"记得配置
 *    sanitize 插件"更硬。
 * 3. **可解释**。解析逻辑（`parseBlocks` / `parseInline`）是两个纯函数，
 *    不依赖 DOM 与浏览器，可以直接在 Node 里断言（见 test/markdown.test.mjs）。
 *
 * ============================================================
 * 支持范围（刻意划定并写清楚）
 * ============================================================
 *
 * 块级：ATX 标题、段落、围栏代码块、有序/无序列表（支持嵌套）、
 *       引用、分割线、表格（含对齐）
 * 行内：行内代码、粗体、斜体、删除线、链接、裸 URL、反斜杠转义
 *
 * 不支持（也故意不做）：HTML 内联、脚注、数学公式、图片语法以外的复杂扩展。
 * 遇到不认识的语法一律**降级为纯文本**，绝不猜 —— 宁可少渲染，不要渲染错。
 *
 * 另外这里的解析器比 CommonMark **宽松**：不强制块之间留空行。
 * 理由是 LLM 生成的文本经常是"标题下一行紧跟着正文"，
 * 严格按规范会把整段当成一个段落渲染出来，观感很差。
 */

// ============================================================
// 类型
// ============================================================

export type Inline =
  | { t: 'text'; v: string }
  | { t: 'code'; v: string }
  | { t: 'strong'; children: Inline[] }
  | { t: 'em'; children: Inline[] }
  | { t: 'del'; children: Inline[] }
  | { t: 'link'; href: string; children: Inline[] }

export type Align = 'left' | 'center' | 'right'

export interface ListItem {
  /** 该条目的首行内容。 */
  content: Inline[]
  /** 缩进更深的部分（可能包含嵌套列表、段落）。 */
  children: Block[]
}

export type Block =
  | { t: 'heading'; level: number; content: Inline[] }
  | { t: 'paragraph'; content: Inline[] }
  | { t: 'code'; lang: string; value: string }
  | { t: 'list'; ordered: boolean; start: number; items: ListItem[] }
  | { t: 'table'; head: Inline[][]; rows: Inline[][][]; align: Align[] }
  | { t: 'quote'; children: Block[] }
  | { t: 'hr' }

// ============================================================
// 行内解析
// ============================================================

/* sticky 正则（y 标志）配合显式设置 lastIndex 使用。
   注意：parseInline 会递归调用自己（粗体里的内容还要再解一遍行内语法），
   而递归会踩到 lastIndex。所以下面一律通过 at() 在 exec 前重设，
   避免"外层匹配位置被内层递归污染"这类极隐蔽的错位。 */
const RE_CODE = /`([^`\n]+)`/y
const RE_STRONG = /\*\*(\S(?:[\s\S]*?\S)?)\*\*/y
const RE_STRONG_ALT = /__(\S(?:[\s\S]*?\S)?)__/y
const RE_EM_STAR = /\*(\S(?:[\s\S]*?\S)?)\*/y
const RE_EM_UNDER = /_(\S(?:[\s\S]*?\S)?)_/y
const RE_DEL = /~~(\S(?:[\s\S]*?\S)?)~~/y
const RE_LINK = /\[([^\]\n]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)/y
const RE_AUTOLINK = /https?:\/\/[^\s<>()[\]]+/y

function at(re: RegExp, src: string, index: number): RegExpExecArray | null {
  re.lastIndex = index
  return re.exec(src)
}

/** 递归解析一段行内文本。 */
export function parseInline(src: string): Inline[] {
  const nodes: Inline[] = []
  let buffer = ''
  let i = 0

  const flush = (): void => {
    if (buffer) {
      nodes.push({ t: 'text', v: buffer })
      buffer = ''
    }
  }

  while (i < src.length) {
    const ch = src[i]

    // ---- 反斜杠转义：\* 应当原样显示星号 ----
    if (ch === '\\' && i + 1 < src.length) {
      buffer += src[i + 1]
      i += 2
      continue
    }

    if (ch === '`') {
      const m = at(RE_CODE, src, i)
      if (m && m[1] !== undefined) {
        flush()
        nodes.push({ t: 'code', v: m[1] })
        i += m[0].length
        continue
      }
    }

    if (ch === '*' || ch === '_') {
      const strong = ch === '*' ? at(RE_STRONG, src, i) : at(RE_STRONG_ALT, src, i)
      if (strong && strong[1] !== undefined) {
        flush()
        nodes.push({ t: 'strong', children: parseInline(strong[1]) })
        i += strong[0].length
        continue
      }
      const em = ch === '*' ? at(RE_EM_STAR, src, i) : at(RE_EM_UNDER, src, i)
      if (em && em[1] !== undefined) {
        flush()
        nodes.push({ t: 'em', children: parseInline(em[1]) })
        i += em[0].length
        continue
      }
      // 匹配不上就退化成普通字符 —— "2 * 3 * 4" 不会被渲染成斜体
      // （正则里要求开闭标记内侧是非空白字符，正是为了让这种写法不误判）
    }

    if (ch === '~') {
      const del = at(RE_DEL, src, i)
      if (del && del[1] !== undefined) {
        flush()
        nodes.push({ t: 'del', children: parseInline(del[1]) })
        i += del[0].length
        continue
      }
    }

    if (ch === '[') {
      const link = at(RE_LINK, src, i)
      if (link && link[1] !== undefined && link[2] !== undefined) {
        const href = safeHref(link[2])
        if (href) {
          flush()
          nodes.push({ t: 'link', href, children: parseInline(link[1]) })
          i += link[0].length
          continue
        }
      }
    }

    if (ch === 'h') {
      const url = at(RE_AUTOLINK, src, i)
      if (url) {
        flush()
        nodes.push({ t: 'link', href: url[0], children: [{ t: 'text', v: url[0] }] })
        i += url[0].length
        continue
      }
    }

    buffer += ch
    i += 1
  }

  flush()
  return nodes
}

/**
 * 只放行 http/https/mailto/相对路径。
 *
 * 为什么必须过滤：`[点我](javascript:alert(1))` 是经典的 Markdown XSS 向量。
 * 这里输出的是 React 元素，React 本身会拦住 `javascript:` 这类 href，
 * 但"依赖下游的兜底"不是安全策略 —— 在入口拒绝才是。
 */
function safeHref(raw: string): string | null {
  const href = raw.trim()
  if (href === '') return null
  if (/^(https?:|mailto:|tel:|\/|#|\.)/i.test(href)) return href
  return null
}

// ============================================================
// 块级解析
// ============================================================

const RE_FENCE = /^\s*(?:```|~~~)\s*([\w+#.-]*)\s*$/
const RE_HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/
const RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/
const RE_QUOTE = /^\s*>\s?(.*)$/
const RE_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/
const RE_TABLE_DELIM = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/

function isBlank(line: string): boolean {
  return line.trim() === ''
}

function indentOf(line: string): number {
  const m = /^\s*/.exec(line)
  return m ? m[0].length : 0
}

/** 一行是否**开启**一个新的块（用于决定段落在哪里结束）。 */
function startsBlock(line: string): boolean {
  if (isBlank(line)) return true
  return (
    RE_FENCE.test(line) ||
    RE_HEADING.test(line) ||
    RE_HR.test(line) ||
    RE_QUOTE.test(line) ||
    RE_ITEM.test(line)
  )
}

/** 把源码切成行。`\r\n` / `\r` 都要处理（模型输出偶尔带 CRLF）。 */
export function splitLines(source: string): string[] {
  return source.replace(/\r\n?/g, '\n').split('\n')
}

export function parseBlocks(source: string): Block[] {
  return parseLines(splitLines(source))
}

function parseLines(lines: string[]): Block[] {
  const blocks: Block[] = []
  let i = 0

  while (i < lines.length) {
    const line = lines[i]
    if (line === undefined || isBlank(line)) {
      i += 1
      continue
    }

    // ---------- 围栏代码块 ----------
    const fence = RE_FENCE.exec(line)
    if (fence) {
      const marker = line.trim().slice(0, 3)
      const lang = (fence[1] ?? '').trim()
      const body: string[] = []
      i += 1
      while (i < lines.length) {
        const current = lines[i] ?? ''
        if (current.trim().startsWith(marker)) {
          i += 1
          break
        }
        body.push(current)
        i += 1
      }
      // 去掉结尾多出来的一个空行：源码通常以换行结尾，
      // 不处理的话每个代码块在 <pre> 里都会多渲染一行空白。
      let value = body.join('\n')
      if (value.endsWith('\n')) value = value.slice(0, -1)
      blocks.push({ t: 'code', lang, value })
      continue
    }

    // ---------- 分割线 ----------
    if (RE_HR.test(line)) {
      blocks.push({ t: 'hr' })
      i += 1
      continue
    }

    // ---------- 标题 ----------
    const heading = RE_HEADING.exec(line)
    if (heading && heading[1] !== undefined) {
      blocks.push({
        t: 'heading',
        level: heading[1].length,
        content: parseInline(heading[2] ?? ''),
      })
      i += 1
      continue
    }

    // ---------- 引用 ----------
    if (RE_QUOTE.test(line)) {
      const inner: string[] = []
      while (i < lines.length) {
        const current = lines[i] ?? ''
        const m = RE_QUOTE.exec(current)
        if (!m) {
          // 引用里的空行允许"懒续行"，但空行之后必须仍是引用才继续
          if (isBlank(current)) {
            const next = lines[i + 1]
            if (next !== undefined && RE_QUOTE.test(next)) {
              inner.push('')
              i += 1
              continue
            }
          }
          break
        }
        inner.push(m[1] ?? '')
        i += 1
      }
      blocks.push({ t: 'quote', children: parseLines(inner) })
      continue
    }

    // ---------- 表格（必须是"表头 + 分隔行"才算） ----------
    const next = lines[i + 1]
    if (line.includes('|') && next !== undefined && next.includes('|') && RE_TABLE_DELIM.test(next)) {
      const head = splitRow(line)
      const align = splitRow(next).map(parseAlign)
      const rows: Inline[][][] = []
      i += 2
      while (i < lines.length) {
        const current = lines[i] ?? ''
        if (isBlank(current) || !current.includes('|')) break
        rows.push(splitRow(current).map((cell) => parseInline(cell)))
        i += 1
      }
      const width = head.length
      blocks.push({
        t: 'table',
        head: head.map((cell) => parseInline(cell)),
        rows: rows.map((row) => normalizeCells(row, width)),
        align: normalizeAlign(align, width),
      })
      continue
    }

    // ---------- 列表 ----------
    if (RE_ITEM.test(line)) {
      const parsed = parseList(lines, i)
      blocks.push(parsed.block)
      i = parsed.next
      continue
    }

    // ---------- 段落 ----------
    const paragraph: string[] = []
    while (i < lines.length) {
      const current = lines[i] ?? ''
      if (isBlank(current)) break
      // 段落中途遇到新块就断开（宽松处理：不需要空行分隔）
      if (paragraph.length > 0 && startsBlock(current)) break
      paragraph.push(current.trim())
      i += 1
    }
    blocks.push({ t: 'paragraph', content: parseInline(paragraph.join(' ')) })
  }

  return blocks
}

/** 拆单元格：支持 `\|` 转义，允许行首行尾省略竖线。 */
function splitRow(line: string): string[] {
  let text = line.trim()
  if (text.startsWith('|')) text = text.slice(1)
  if (text.endsWith('|') && !text.endsWith('\\|')) text = text.slice(0, -1)

  const cells: string[] = []
  let current = ''
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i]
    if (ch === '\\' && text[i + 1] === '|') {
      current += '|'
      i += 1
      continue
    }
    if (ch === '|') {
      cells.push(current.trim())
      current = ''
      continue
    }
    current += ch
  }
  cells.push(current.trim())
  return cells
}

function parseAlign(cell: string): Align {
  const value = cell.trim()
  const left = value.startsWith(':')
  const right = value.endsWith(':')
  if (left && right) return 'center'
  if (right) return 'right'
  return 'left'
}

function normalizeCells(row: Inline[][], width: number): Inline[][] {
  const out = row.slice(0, width)
  while (out.length < width) out.push([])
  return out
}

function normalizeAlign(align: Align[], width: number): Align[] {
  const out = align.slice(0, width)
  while (out.length < width) out.push('left')
  return out
}

/**
 * 解析列表，支持缩进嵌套。
 *
 * 做法：先按"基准缩进"切出一级条目；每个条目缩进更深的部分
 * **递归**交给 parseLines —— 于是嵌套列表、条目里的段落、甚至代码块
 * 都能自然支持，不需要为每种组合写分支。
 */
function parseList(lines: string[], start: number): { block: Block; next: number } {
  const firstLine = lines[start] ?? ''
  const baseIndent = indentOf(firstLine)
  const firstMatch = RE_ITEM.exec(firstLine)
  const ordered = firstMatch?.[2] !== undefined && /\d/.test(firstMatch[2])
  const startNumber = ordered && firstMatch?.[2] ? Number.parseInt(firstMatch[2], 10) || 1 : 1

  const items: ListItem[] = []
  let i = start
  let currentFirst = ''
  let currentChildren: string[] = []

  const flushItem = (): void => {
    if (currentFirst === '' && currentChildren.length === 0) return
    items.push({
      content: parseInline(currentFirst.trim()),
      children: currentChildren.length > 0 ? parseLines(dedent(currentChildren, baseIndent + 2)) : [],
    })
    currentFirst = ''
    currentChildren = []
  }

  while (i < lines.length) {
    const line = lines[i] ?? ''

    if (isBlank(line)) {
      // 空行后如果还有更深的缩进，说明条目没结束；否则整个列表结束
      const nextLine = lines[i + 1]
      if (nextLine !== undefined && !isBlank(nextLine) && indentOf(nextLine) > baseIndent) {
        currentChildren.push('')
        i += 1
        continue
      }
      break
    }

    const indent = indentOf(line)
    const match = RE_ITEM.exec(line)

    if (indent <= baseIndent && match) {
      // 同缩进但标记类型变了（`-` ↔ `1.`）说明这是**另一个列表**。
      // 没有这个判断，`- a\n1. b` 会被合成一个无序列表，
      // 编号会作为正文文本混进去。
      const isOrderedItem = /\d/.test(match[2] ?? '')
      if (isOrderedItem !== ordered) break

      flushItem()
      currentFirst = match[3] ?? ''
      i += 1
      continue
    }

    if (indent <= baseIndent && !match) break // 回到了列表外的块

    // 更深的缩进 → 属于当前条目
    currentChildren.push(line)
    i += 1
  }
  flushItem()

  return { block: { t: 'list', ordered, start: startNumber, items }, next: i }
}

function dedent(lines: string[], amount: number): string[] {
  return lines.map((line) => {
    let removed = 0
    let index = 0
    while (index < line.length && removed < amount && (line[index] === ' ' || line[index] === '\t')) {
      removed += line[index] === '\t' ? 4 : 1
      index += 1
    }
    return line.slice(index)
  })
}
