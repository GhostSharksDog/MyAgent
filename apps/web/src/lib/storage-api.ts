import { apiUrl } from './api'
import { accessHeaders } from './access'

export interface MemoryFact { id: string; text: string; tags: string[]; ts: number }
export interface MemoryList { enabled: boolean; backend: string; max_facts: number; facts: MemoryFact[] }
export interface StorageStatus {
  data_directory: string
  sessions: { backend: string; persistent: boolean; ttl_seconds: number }
  memory: { active_backend: string; active_enabled: boolean; enable_summary: boolean } | null
  runs: { active_backend: string } | null
}
export async function storageRequest<T>(path: string, method = 'GET', payload?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(apiUrl('/api/' + path), { method, signal, headers: { ...accessHeaders(), 'Content-Type': 'application/json' }, body: payload === undefined ? undefined : JSON.stringify(payload) })
  const value = await response.json()
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '操作失败，请检查配置后重试')
  return value as T
}
