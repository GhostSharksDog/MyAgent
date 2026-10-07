/** 终端等待与执行都必须有有限时限；允许小数秒，与服务端范围一致。 */
export function parseTerminalTimeout(value: string, maximum: 600 | 3600): number | null {
  if (!value.trim()) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 1 && parsed <= maximum ? parsed : null
}
