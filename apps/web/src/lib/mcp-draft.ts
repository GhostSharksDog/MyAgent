import type { MCPConfig } from './mcp'
import { newMCPConfig } from './mcp'

export type MCPPreset = NonNullable<MCPConfig['preset']>
export const MCP_PRESETS: { id: MCPPreset; label: string }[] = [
  { id: 'tavily', label: 'Tavily 搜索' }, { id: 'exa', label: 'Exa 搜索' },
  { id: 'filesystem', label: '本地文件' }, { id: 'desktop_commander', label: 'Desktop Commander' },
  { id: 'custom', label: '自定义服务' },
]
export function detectPreset(config: MCPConfig): MCPPreset {
  if (config.preset) return config.preset
  if (config.url.startsWith('https://mcp.tavily.com/')) return 'tavily'
  if (config.url.startsWith('https://mcp.exa.ai/')) return 'exa'
  if (config.args.some(v => /server-filesystem/.test(v))) return 'filesystem'
  if (config.args.some(v => /desktop-commander/.test(v))) return 'desktop_commander'
  return 'custom'
}
export function presetDraft(preset: MCPPreset): MCPConfig {
  const draft = newMCPConfig(preset === 'exa')
  draft.preset = preset
  draft.name = MCP_PRESETS.find(v => v.id === preset)?.label ?? ''
  if (preset === 'tavily') {
    draft.url = 'https://mcp.tavily.com/mcp/'
    draft.selected_tools = ['tavily-search', 'tavily-extract']
  }
  if (preset === 'filesystem' || preset === 'desktop_commander') draft.transport = 'stdio'
  return draft
}
export function applySecret(headers: Record<string, string>, key: string, auth: 'none' | 'bearer' | 'header', name: string, clear: boolean) {
  const result = { ...headers }
  const target = auth === 'bearer' ? 'Authorization' : name.trim()
  if (clear) {
    if (target) delete result[target]
    delete result.Authorization
  } else if (auth !== 'none' && key.trim()) {
    if (!target || !/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(target)) throw new Error('请填写有效的鉴权头名称')
    result[target] = auth === 'bearer' ? `Bearer ${key.trim()}` : key.trim()
  }
  return result
}
