/**
 * 访问密钥：客户端这一半（后端那一半见 services/api/app/api/auth.py）。
 *
 * ============================================================
 * 为什么密钥存在**浏览器**里，而不是服务端配置里
 * ============================================================
 * 服务端配置里那份是"要求什么密钥"，而这里这份是"我用哪个密钥去访问"。
 * 两者是不同的东西：
 *
 *   服务端 `SECURITY_API_KEY`  → 决定谁可以调用
 *   浏览器 localStorage 里的键  → 你可能访问多个部署，各自密钥不同
 *
 * 所以它**不进设置面板的服务端那一半**（那个面板写的是 .env），
 * 而是纯客户端状态。这也意味着它不会被 `PUT /api/settings` 带走，
 * 不会出现在任何请求体里 —— 它只出现在请求头里。
 *
 * ============================================================
 * 为什么不放进 URL 参数或 Cookie
 * ============================================================
 *   URL 参数 → 会进浏览器历史、Referer、以及服务端访问日志
 *   Cookie   → 会随每个请求自动发出（包括跨站请求），且需要 CSRF 防护
 *   请求头   → 只在需要的地方显式带上，不落任何日志（本项目的日志只记
 *              方法与路径，不记请求头）
 *
 * 结论：请求头是这三者里唯一"不会自己泄漏"的载体。
 */

const STORAGE_KEY = 'legacy.accessKey'

/** 取 localStorage；在非浏览器环境（测试/SSR）返回 null 而不是抛错。 */
function storage(): Storage | null {
  try {
    if (typeof localStorage === 'undefined') return null
    return localStorage
  } catch {
    // 隐私模式下访问 localStorage 会抛异常 —— 那不该让整个应用挂掉，
    // 只是这次访问拿不到已保存的密钥而已
    return null
  }
}

/** 读取已保存的密钥（没有则空串）。 */
export function getAccessKey(): string {
  return storage()?.getItem(STORAGE_KEY)?.trim() ?? ''
}

/** 保存密钥；空串表示清除（用户把输入框清空即为"不再使用密钥"）。 */
export function setAccessKey(key: string): void {
  const store = storage()
  if (!store) return
  const trimmed = key.trim()
  if (trimmed) store.setItem(STORAGE_KEY, trimmed)
  else store.removeItem(STORAGE_KEY)
}

/**
 * 把密钥转成请求头。没有密钥时返回空对象。
 *
 * 【为什么用 `X-API-Key` 而不是 `Authorization: Bearer`】
 * 后端两种都收（标准写法与企业网关更常见的 Bearer 都支持），
 * 但浏览器这边选 X-API-Key：`Authorization` 是浏览器自己会管理的头
 * （HTTP 认证弹窗、某些代理会改写它），自定义头不会与任何内置行为冲突。
 */
export function accessHeaders(key: string = getAccessKey()): Record<string, string> {
  return key ? { 'X-API-Key': key } : {}
}

// ============================================================
// "该不该提示用户去填密钥"
// ============================================================
export type AccessNotice = 'none' | 'required' | 'configured'

/**
 * 从"服务端要不要密钥"和"本地有没有密钥"推出界面该显示什么。
 *
 * 三态而不是布尔，因为这里有三件不同的事：
 *   none       服务没启用密钥 → 界面上什么都不该提（本地开发的常态）
 *   required   服务要、本地没有 → **必须提示**，否则用户只会看到一堆 401
 *   configured 服务要、本地有 → 安静地工作，不打扰
 */
export function resolveAccessNotice(authRequired: boolean, hasKey: boolean): AccessNotice {
  if (!authRequired) return 'none'
  return hasKey ? 'configured' : 'required'
}
