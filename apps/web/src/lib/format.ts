/**
 * 纯格式化工具。
 *
 * 单独成文件的原因：这些函数是"显示层"的全部可测逻辑，
 * 不依赖 React、不依赖 DOM，测起来零成本。
 *
 * 统一的取舍：**信息密度优先于好看**。
 * 例如 token 数用 `1.2k` 而不是 `1,234`，时长用 `340ms` / `1.4s` 而不是
 * `0.34 秒` —— 这些数字是运维/成本视角的，越紧凑越容易一眼扫过。
 */

/** 后端所有时间戳都是 `time.time()` 秒级浮点数。 */
export function formatRelativeTime(timestampSeconds: number): string {
  const now = Date.now() / 1000
  const diff = Math.max(0, now - timestampSeconds)

  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`

  const date = new Date(timestampSeconds * 1000)
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

export function formatClock(timestampSeconds: number): string {
  const date = new Date(timestampSeconds * 1000)
  const hh = String(date.getHours()).padStart(2, '0')
  const mm = String(date.getMinutes()).padStart(2, '0')
  return `${hh}:${mm}`
}

export function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

/** 千位以内显示原值，超过则用 k/m 缩写 —— 状态栏空间紧张。 */
export function formatCompact(value: number): string {
  if (value < 1000) return String(value)
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`
  return `${(value / 1_000_000).toFixed(1)}m`
}

export function formatDurationMs(ms: number | null): string {
  if (ms === null) return '—'
  if (ms < 1) return '<1 ms'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

/** 会话 id 太长，列表里只给前 8 位（uuid4 的熵足够，重复概率可忽略）。 */
export function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id
}

/**
 * 把工具参数压成一行 `key=value`摘要，用于卡片标题行。
 * 完整 JSON 仍然可以在展开后看到 —— 摘要只是为了让用户不必展开就能读懂。
 */
export function summarizeToolArgs(args: Record<string, unknown>, maxLength = 72): string {
  const parts: string[] = []
  for (const [key, value] of Object.entries(args)) {
    let text: string
    if (typeof value === 'string') text = value
    else if (value === null || value === undefined) text = 'null'
    else text = JSON.stringify(value)

    if (text.length > 40) text = `${text.slice(0, 40)}…`
    parts.push(`${key}=${text}`)
  }

  const joined = parts.join('  ')
  if (joined.length <= maxLength) return joined
  return `${joined.slice(0, maxLength)}…`
}

/** 判断工具输出是不是"结构化的多行内容"，决定用 <pre> 还是纯文本渲染。 */
export function looksLikeJson(text: string): boolean {
  const trimmed = text.trim()
  return (
    (trimmed.startsWith('{') && trimmed.endsWith('}')) ||
    (trimmed.startsWith('[') && trimmed.endsWith(']'))
  )
}
