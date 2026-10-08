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

import { useRef, useState } from 'react'

import { useDialogFocus } from '../hooks/useDialogFocus'
import type { ApiMeta, HealthStatus, ToolInfo } from '../lib/types'
import { IconChevronRight, IconGrid, IconRefresh, IconX } from './Icons'

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
          {tool.source === 'mcp' && <span className="toolitem__desc">MCP · {tool.server_name} / {tool.remote_name}</span>}
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
  health: HealthStatus | null
  sessionId: string | null
  onRefresh: () => void
  onClose: () => void
}

export function ToolsDrawer({ open, tools, meta, health, sessionId, onRefresh, onClose }: ToolsDrawerProps) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const closeRef = useRef<HTMLButtonElement | null>(null)
  useDialogFocus({ open, containerRef, onClose, initialFocusRef: closeRef })

  if (!open) return null
  const backend = health?.session_backend ?? meta?.session_backend
  const storage = backend === 'memory'
    ? '内存 · 重启后历史不会保留'
    : backend === 'sqlite'
      ? 'SQLite · 历史保存在本机数据库'
      : backend === 'sql'
        ? 'SQL · 历史保存在配置的数据库'
        : backend === 'postgresql'
          ? 'PostgreSQL · 历史保存在配置的数据库'
          : backend === 'redis'
            ? 'Redis · 由 Redis 服务保存历史'
            : backend ? `${backend} · 以服务端配置为准` : '状态尚未读取'

  return (
    <>
      <button
        type="button"
        className="drawer-scrim"
        aria-label="关闭工具面板"
        tabIndex={-1}
        onClick={onClose}
      />
      <div ref={containerRef} className="drawer" role="dialog" aria-modal="true" aria-label="已注册的工具" tabIndex={-1}>
        <div className="drawer__head">
          <IconGrid size={15} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="drawer__title">工具与服务</div>
            <div className="drawer__subtitle">
              当前已注册 {tools.length} 个工具
            </div>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="btn btn--ghost btn--icon"
            onClick={onClose}
            aria-label="关闭工具面板"
          >
            <IconX size={15} />
          </button>
        </div>

        <div className="drawer__body">
          <div className="drawer__note">
            这里列出当前模型可调用的能力。展开工具可查看参数、说明与完整 Schema。
          </div>
          {tools.length === 0 ? (
            <div className="sidebar__empty">暂未取得工具清单。刷新服务状态可重新读取。</div>
          ) : (
            tools.map((tool) => <ToolItem key={tool.name} tool={tool} />)
          )}
          <details className="drawer__details">
            <summary>服务详情</summary>
            <dl className="drawer__facts">
              <div><dt>服务状态</dt><dd>{health?.status ?? '尚未读取'}</dd></div>
              <div><dt>模型</dt><dd>{health?.model ?? meta?.model ?? '尚未读取'}</dd></div>
              <div><dt>模型配置</dt><dd>{health ? health.llm_configured ? '已配置' : '尚未配置' : '状态未知'}</dd></div>
              <div><dt>访问控制</dt><dd>{health ? health.auth_required ? '需要访问密钥' : '本地无需密钥' : '状态未知'}</dd></div>
              <div><dt>会话存储</dt><dd>{storage}</dd></div>
              <div><dt>当前会话</dt><dd>{sessionId ?? '未选择 · 不保存此次对话'}</dd></div>
              <div><dt>最大执行步数</dt><dd>{meta ? `${meta.max_steps} 步` : '尚未读取'}</dd></div>
              <div><dt>版本与环境</dt><dd>{meta ? `${meta.service} ${meta.version} · ${meta.env}` : health?.env ?? '尚未读取'}</dd></div>
              <div><dt>工具接口</dt><dd><code>GET /api/tools</code></dd></div>
            </dl>
            <button type="button" className="btn btn--ghost drawer__refresh" onClick={onRefresh}>
              <IconRefresh size={14} /> 刷新服务状态
            </button>
          </details>
        </div>
      </div>
    </>
  )
}
