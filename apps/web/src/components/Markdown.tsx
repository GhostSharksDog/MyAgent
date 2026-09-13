/**
 * Markdown 渲染组件。
 *
 * ============================================================
 * 关键设计：输出 React 元素，而不是 HTML 字符串
 * ============================================================
 *
 * 绝大多数"自研 markdown 渲染"最后都写成 `dangerouslySetInnerHTML`，
 * 于是模型输出里的任何 HTML 都会被浏览器执行 —— 这是 XSS。
 * 本组件把解析结果（Block / Inline 树）逐个映射成 React 元素，
 * **全程没有一个 innerHTML**：即使回答里出现 `<img onerror=...>`，
 * 它也只会被当作普通文本显示出来。安全是结构带来的，不是靠事后过滤。
 *
 * ============================================================
 * 为什么用 useDeferredValue
 * ============================================================
 * 流式输出时每来一个 token 都会重新渲染，而重新解析一遍全文是 O(n)，
 * 一整轮下来就是 O(n²)。`useDeferredValue` 让"解析 + 渲染 Markdown"
 * 变成低优先级的可中断更新：打字机效果的视觉流畅度不受影响，
 * 输入框与滚动也不会被长文本的解析卡住。
 * （这里没有做增量解析 —— 那需要维护解析器中间状态，
 *   复杂度远高于收益；先让它在长回答下不卡，是性价比最高的那一步。）
 */

import { useDeferredValue, useMemo } from 'react'
import type { ReactNode } from 'react'

import { parseBlocks } from '../lib/markdown'
import type { Block, Inline } from '../lib/markdown'

function renderInline(nodes: Inline[], keyPrefix: string): ReactNode[] {
  return nodes.map((node, index) => {
    const key = `${keyPrefix}.${index}`
    switch (node.t) {
      case 'text':
        return <span key={key}>{node.v}</span>
      case 'code':
        return <code key={key}>{node.v}</code>
      case 'strong':
        return <strong key={key}>{renderInline(node.children, key)}</strong>
      case 'em':
        return <em key={key}>{renderInline(node.children, key)}</em>
      case 'del':
        return <del key={key}>{renderInline(node.children, key)}</del>
      case 'link':
        return (
          <a key={key} href={node.href} target="_blank" rel="noreferrer noopener">
            {renderInline(node.children, key)}
          </a>
        )
      default:
        return null
    }
  })
}

function BlockView({ block, id }: { block: Block; id: string }) {
  switch (block.t) {
    case 'heading': {
      // 只映射到 h1~h6，不在回答里出现 h1 的视觉噪音 —— 但保持语义层级
      const Tag = `h${Math.min(Math.max(block.level, 1), 6)}` as 'h1'
      return <Tag>{renderInline(block.content, id)}</Tag>
    }

    case 'paragraph':
      return <p>{renderInline(block.content, id)}</p>

    case 'code':
      return (
        <div className="md__pre-wrap">
          {block.lang ? <span className="md__lang">{block.lang}</span> : null}
          <pre>
            <code>{block.value}</code>
          </pre>
        </div>
      )

    case 'list': {
      const ListTag = block.ordered ? 'ol' : 'ul'
      return (
        <ListTag start={block.ordered && block.start !== 1 ? block.start : undefined}>
          {block.items.map((item, index) => (
            <li key={`${id}.li${index}`}>
              {renderInline(item.content, `${id}.li${index}`)}
              {item.children.length > 0 ? (
                <BlockList blocks={item.children} id={`${id}.li${index}`} />
              ) : null}
            </li>
          ))}
        </ListTag>
      )
    }

    case 'table':
      return (
        <div className="md__table-wrap">
          <table>
            <thead>
              <tr>
                {block.head.map((cell, index) => (
                  <th key={`${id}.h${index}`} style={{ textAlign: block.align[index] ?? 'left' }}>
                    {renderInline(cell, `${id}.h${index}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row, rowIndex) => (
                <tr key={`${id}.r${rowIndex}`}>
                  {row.map((cell, cellIndex) => (
                    <td
                      key={`${id}.r${rowIndex}c${cellIndex}`}
                      style={{ textAlign: block.align[cellIndex] ?? 'left' }}
                    >
                      {renderInline(cell, `${id}.r${rowIndex}c${cellIndex}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )

    case 'quote':
      return (
        <blockquote>
          <BlockList blocks={block.children} id={`${id}.q`} />
        </blockquote>
      )

    case 'hr':
      return <hr />

    default:
      return null
  }
}

function BlockList({ blocks, id }: { blocks: Block[]; id: string }) {
  return (
    <>
      {blocks.map((block, index) => (
        <BlockView key={`${id}.${index}`} block={block} id={`${id}.${index}`} />
      ))}
    </>
  )
}

export interface MarkdownProps {
  source: string
}

export function Markdown({ source }: MarkdownProps) {
  const deferred = useDeferredValue(source)
  const blocks = useMemo(() => parseBlocks(deferred), [deferred])

  return (
    <div className="md">
      <BlockList blocks={blocks} id="md" />
    </div>
  )
}
