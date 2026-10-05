/**
 * 设置与文件浏览的 HTTP 客户端。
 *
 * 【为什么单独一个文件而不是塞进 lib/api.ts】
 * `api.ts` 的定位是"对话相关的主链路"（会话、流式、元信息），
 * 而这个文件是**配置与工具面板**的接口。两者更新节奏不同 ——
 * 设置项的增删远比对话协议频繁，混在一起会让 api.ts 越来越难读。
 *
 * 约定与 api.ts 保持一致（这是更重要的部分）：同样只有这一层能出网、
 * 同样把后端的中文错误信息原样带出来。后端在设置接口里为每种失败
 * 都写了**具体的**提示（"工作区目录不存在"、"密钥无效，请检查是否复制完整"），
 * 前端自己编一句"保存失败"就把这些信息全丢了。
 */

import { accessHeaders } from './access'
import { ApiError, apiUrl } from './api'
import type {
  BrowseListing,
  DirListing,
  LocateResponse,
  FileContent,
  PickResult,
  PickerInfo,
  SettingsUpdatePayload,
  SettingsView,
  TestConnectionResult,
  WorkspaceInfo,
} from './types'

/** 统一的请求封装。错误处理与 api.ts 的 `readErrorDetail` 保持同一套语义。 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        // 与 api.ts 一样带上访问密钥（服务端启用鉴权时它是必需的）
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
      if (typeof body?.detail === 'string') {
        detail = body.detail
      } else if (Array.isArray(body?.detail)) {
        // FastAPI 的 422 校验错误是数组形态，取第一条的 msg
        const first = body.detail[0]
        if (first?.msg) detail = String(first.msg)
      }
    } catch {
      /* 响应体不是 JSON —— 保留默认文案 */
    }
    throw new ApiError(response.status, detail)
  }

  return (await response.json()) as T
}

// ============================================================
// 设置
// ============================================================
export function fetchSettings(): Promise<SettingsView> {
  return request<SettingsView>('/api/settings')
}

export function updateSettings(payload: SettingsUpdatePayload): Promise<SettingsView> {
  return request<SettingsView>('/api/settings', {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function testLLMConnection(): Promise<TestConnectionResult> {
  return request<TestConnectionResult>('/api/settings/test', { method: 'POST' })
}

// ============================================================
// 文件浏览
// ============================================================
export function fetchWorkspace(): Promise<WorkspaceInfo> {
  return request<WorkspaceInfo>('/api/files/workspace')
}

export function listDirectory(path: string, includeHidden = false): Promise<DirListing> {
  const q = new URLSearchParams({ path, include_hidden: String(includeHidden) })
  return request<DirListing>(`/api/files/list?${q.toString()}`)
}

export function readFileContent(path: string): Promise<FileContent> {
  const q = new URLSearchParams({ path })
  return request<FileContent>(`/api/files/content?${q.toString()}`)
}

/**
 * 浏览目录（选工作区用）。
 *
 * ⚠ 这个接口**可以走出工作区**，返回的是**目录名**。
 *
 * 理由：浏览器里没有服务端的文件夹对话框，要选工作区就必须能先看到它 ——
 * 否则用户永远只能选当前工作区里面的文件夹。而它刻意只返回目录、
 * 不返回文件名、不返回内容：**选择工作区需要看到目录名，但不需要看到文件内容。**
 */
export function browseDirectories(path = ''): Promise<BrowseListing> {
  const q = new URLSearchParams({ path })
  return request<BrowseListing>(`/api/files/browse?${q.toString()}`)
}

/**
 * 用「文件夹名 + 若干相对路径」反查绝对路径。
 *
 * ⚠ 这是**旧方案**，只在宿主弹不出系统对话框（browse 后端）时作为兜底出现。
 * 有系统对话框时请用 `pickDirectory()` —— 那条路一步到位、不需要猜。
 *
 * 【为什么需要绕这一圈】
 * `<input type="file" webkitdirectory>` 弹的是**系统**文件夹选择器，
 * 但浏览器出于隐私**剥掉了绝对路径** —— 网页只能拿到 `webkitRelativePath`
 * （形如 `MyAgent/src/App.tsx`）。任何网页都拿不到完整路径，这是浏览器设计，
 * 不是能绕过去的实现细节。
 *
 * 所以：名字用来**筛选**，相对路径用来**确认**。两样加起来基本能唯一定位。
 */
export function locateFolder(name: string, samples: string[]): Promise<LocateResponse> {
  return request<LocateResponse>('/api/files/locate', {
    method: 'POST',
    body: JSON.stringify({ name, samples }),
  })
}

// ============================================================
// 系统文件夹对话框（宿主进程弹窗）
// ============================================================
/**
 * 问服务端：你现在能弹出系统文件夹对话框吗？
 *
 * 【为什么先问再渲染，而不是"点了再说"】
 * 这是一个**启动时就确定的静态事实**（服务绑定在哪张网卡、有没有图形会话）。
 * 让前端去试会把一个可以直接说明的事实变成一次失败的用户操作 ——
 * 用户点了一个按钮，然后什么也没发生。
 */
export function fetchPickerCapability(): Promise<PickerInfo> {
  return request<PickerInfo>('/api/files/picker')
}

/**
 * 让宿主进程弹出系统文件夹对话框，拿回**绝对路径**。
 *
 * 【为什么这里不能设超时】
 * 这个请求会一直挂着，直到用户在系统对话框里点完 —— 可能是几秒，
 * 也可能是几分钟（他去接了个电话）。给它套一个"XX 秒超时"的直觉是错的：
 * 用户明明选好了，界面却报超时失败。`fetch` 默认没有超时，正合适。
 *
 * 【为什么要发一个空 JSON 体】
 * 空 body 的 POST 属于 CORS 简单请求，任何第三方网页都能让浏览器发出它；
 * 而带上 `Content-Type: application/json` 会触发预检，非本机的来源会被拒。
 * 这个接口的副作用是**在用户屏幕上弹窗**，所以这一点是刻意的。
 */
export function pickDirectory(): Promise<PickResult> {
  return request<PickResult>('/api/files/pick', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}
