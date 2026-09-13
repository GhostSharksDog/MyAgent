/**
 * 工具面板（右侧抽屉）：把 `/api/tools` 返回的工具表展开给用户看。
 *
 * ============================================================
 * 为什么值得单独做一个面板
 * ============================================================
 * "Agent 能干什么"完全由**注册了哪些工具**决定，而工具描述的质量
 * 又直接决定模型会不会正确使用它 —— 这是 Agent 工程里最关键、
 * 也最少被展示的一层。
 *
 * 所以这里不止列名字和一句话：
 *   - 参数表是从工具的 JSON Schema 里解出来的（Pydantic 模型自动生成），
 *     包括类型、是否必填、字段说明；
 *   - 展开还能看到原始 Schema。参数描述写得好不好，一眼就能判断。
 *
 * 把这些摊开来看，比在 README 里写"支持工具调用"有说服力得多。
 *
 * ============================================================
 * 无障碍
 * ============================================================
 * 抽屉是一个 dialog：打开时按 Esc 关闭、点击遮罩关闭、
 * 打开后焦点移到关闭按钮（否则键盘用户会困在背后的页面上）。
 * 这些成本很低，但缺了就是"能用"和"做完"的区别。
 */

import { useEffect, useRef, useState } from 'react'

import type { ApiMeta, ToolInfo } from '../lib/types'
import { IconChevronRight, IconGrid, IconX } from './Icons'

/** 从 JSON Schema 里读出一个简短的参数类型描述。
 *  Pydantic v2 对 Optional 字段会产出 `anyOf: [{type: 'string'}, {type: 'null'}]`，
 *  对枚举会产出 `enum: [...]` —— 直接显示 `object` 会丢信息，所以这里都展开。 */
function describeType(schema: unknown): string {
  if (schema === null || typeof schema !== 'object') return 'any'
  const node = schema as Record<string, unknown>

  if (Array.isArray(node['enum'])) {
    return node['enum'].map((value) => JSON.stringify(value)).join(' | ')
  }

  if (Array.isArray(node['anyOf'])) {
    const parts = (node['anyOf'] as unknown[]).map(describeType).filter((text) => text !== 'null')
    return parts.join(' | ') || 'any'
  }

  const type = node['type']
  if (typeof type === 'string') {
    if (type === 'array' && node['items']) return `${describeType(node['items'])}[]`
    return type
  }

  return 'any'
}

function ToolItem({ tool }: { tool: ToolInfo }) {
  const [open, setOpen] = useState(false)

  const properties =
    tool.parameters && typeof tool.parameters['properties'] === 'object'
      ? (tool.parameters['properties'] as Record<string, unknown>)
      : {}
  const required = Array.isArray(tool.parameters['required'])
    ? (tool.parameters['required'] as string[])
    : []
  const entries = Object.entries(properties)

  return (
    <section className="toolitem" data-open={open}>
      <button
        type="button"
        className="toolitem__head"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span className="toolitem__title">
          <span className="toolitem__name">
            {tool.name}
            <span className="muted mono" style={{ fontWeight: 400, fontSize: 'var(--fs-xs)' }}>
              {entries.length} 个参数
              {required.length > 0 ? ` · ${required.length} 必填` : ''}
            </span>
          </span>
          <span className="toolitem__desc">{tool.description}</span>
        </span>
        <span className="toolitem__chevron">
          <IconChevronRight size={12} />
        </span>
      </button>

      {open ? (
        <div className="toolitem__body">
          {entries.length === 0 ? (
            <span className="toolcard__empty">该工具不需要参数</span>
          ) : (
            <div className="schema-table">
              {entries.map(([name, schema]) => {
                const node = (schema ?? {}) as Record<string, unknown>
                const description = typeof node['description'] === 'string' ? node['description'] : ''
                const isRequired = required.includes(name)
                return (
                  <div className="schema-row" key={name}>
                    <span className="schema-row__name">
                      {name}
                      {isRequired ? <span className="schema-row__required"> *</span> : null}
                    </span>
                    <span className="schema-row__type">{describeType(node)}</span>
                    <span className="schema-row__desc">{description || '—'}</span>
                  </div>
                )
              })}
            </div>
          )}

          <div>
            <div className="toolcard__section-label">原始 JSON Schema</div>
            <pre className="toolitem__raw">{JSON.stringify(tool.parameters, null, 2)}</pre>
          </div>
        </div>
      ) : null}
    </section>
  )
}

export interface ToolsDrawerProps {
  open: boolean
  tools: ToolInfo[]
  meta: ApiMeta | null
  onClose: () => void
}

export function ToolsDrawer({ open, tools, meta, onClose }: ToolsDrawerProps) {
  const closeRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    if (!open) return undefined
    closeRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, onClose])

  if (!open) return null

  return (
    <>
      <button
        type="button"
        className="drawer-scrim"
        aria-label="关闭工具面板"
        onClick={onClose}
      />
      <div className="drawer" role="dialog" aria-modal="true" aria-label="已注册的工具">
        <div className="drawer__head">
          <IconGrid size={15} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="drawer__title">已注册的工具</div>
            <div className="drawer__subtitle">
              GET /api/tools · 共 {tools.length} 个
              {meta ? ` · max_steps=${meta.max_steps}` : ''}
            </div>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="btn btn--ghost btn--icon"
            onClick={onClose}
            aria-label="关闭"
          >
            <IconX size={15} />
          </button>
        </div>

        <div className="drawer__body">
          <div className="drawer__note">
            这些工具就是模型能调用的全部能力。描述与参数 Schema 会原样发给模型 ——
            描述写得含糊，模型就会乱调或漏调，所以工具层决定了 Agent 可靠性的一半。
          </div>
          {tools.length === 0 ? (
            <div className="sidebar__empty">没有工具（或 /api/tools 加载失败）。</div>
          ) : (
            tools.map((tool) => <ToolItem key={tool.name} tool={tool} />)
          )}
        </div>
      </div>
    </>
  )
}
