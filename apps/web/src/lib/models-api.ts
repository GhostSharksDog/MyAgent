/**
 * 模型管理的 HTTP 客户端。
 *
 * 约定与 `settings-api.ts` 一致（这是更重要的部分）：只有这一层出网、
 * 把后端的中文错误信息原样带出来、并带上访问密钥（服务端启用鉴权时它是必需的）。
 *
 * 纯逻辑（预设、草稿、校验、展示文案）在 `models-view.ts` ——
 * 见那个文件头关于"为什么拆开"的说明。
 */

import { accessHeaders } from './access'
import { ApiError, apiUrl } from './api'
import type { ModelListResponse, ModelSavePayload } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...accessHeaders(),
        ...(init?.headers ?? {}),
      },
    })
  } catch {
    // 连接层失败（后端没起来）—— status 0 让调用方能区分"服务不可用"
    // 与"请求被拒绝"，这两种情况的提示文案完全不同
    throw new ApiError(0, '无法连接后端服务')
  }

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') detail = body.detail
      else if (Array.isArray(body?.detail) && body.detail[0]?.msg) {
        // FastAPI 的 422 校验错误是数组形态，取第一条的 msg
        detail = String(body.detail[0].msg)
      }
    } catch {
      /* 响应不是 JSON，用默认文案 */
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}

export function fetchModels(): Promise<ModelListResponse> {
  return request<ModelListResponse>('/api/models')
}

export function saveModel(payload: ModelSavePayload): Promise<ModelListResponse> {
  return request<ModelListResponse>('/api/models', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function deleteModel(id: string): Promise<ModelListResponse> {
  return request<ModelListResponse>(`/api/models/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

/** 切换当前使用的模型。后端会写 `.env` 并**重建 Agent 全栈**（立即生效）。 */
export function activateModel(id: string): Promise<ModelListResponse> {
  return request<ModelListResponse>(`/api/models/${encodeURIComponent(id)}/activate`, {
    method: 'POST',
  })
}

/** 把当前 `.env` 里的配置存进清单（界面拿不到密钥原文，只能由后端代劳）。 */
export function importCurrentModel(label: string): Promise<ModelListResponse> {
  return request<ModelListResponse>('/api/models/import-current', {
    method: 'POST',
    body: JSON.stringify({ label }),
  })
}
