import { apiUrl } from './api'
import { accessHeaders } from './access'

export interface MCPConfig {
  preset?: 'tavily' | 'exa' | 'filesystem' | 'desktop_commander' | 'custom'
  id?: string
  name: string
  transport: 'http' | 'stdio'
  enabled: boolean
  url: string
  command: string
  args: string[]
  cwd: string
  env: Record<string, string>
  headers: Record<string, string>
  proxy: string
  selected_tools: string[]
}
export interface MCPToolView { name: string; description: string; selected: boolean; trusted: boolean; trust_allowed: boolean; error: string }
export interface MCPServerView extends MCPConfig { id: string; status: string; error: string; protocol: string; tools: MCPToolView[]; secret_configured?: boolean; available_tool_count?: number; missing_tools?: string[] }
export interface MCPView { enabled: boolean; error: string; max_tools: number; servers: MCPServerView[] }

export function newMCPConfig(exa = false): MCPConfig {
  return { name: exa ? 'Exa 联网' : '', transport: 'http', enabled: false,
    url: exa ? 'https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa' : '',
    command: '', args: [], cwd: '', env: {}, headers: {}, proxy: '',
    selected_tools: exa ? ['web_search_exa', 'web_fetch_exa'] : [] }
}

export function stringMap(text: string): Record<string, string> {
  const value: unknown = JSON.parse(text || '{}')
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.values(value).some(v => typeof v !== 'string'))
    throw new Error('请填写 JSON 对象，名称和值都须为字符串')
  return value as Record<string, string>
}

export async function mcpRequest(path = '', method = 'GET', payload?: unknown, signal?: AbortSignal): Promise<MCPView> {
  const response = await fetch(apiUrl('/api/mcp' + path), { method, signal,
    headers: { ...accessHeaders(), 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload) })
  const value = await response.json()
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'MCP 请求失败，请检查配置后重试')
  if (!value || !Array.isArray(value.servers) || typeof value.enabled !== 'boolean') throw new Error('MCP 状态响应无效，请更新服务后重试')
  return value as MCPView
}
