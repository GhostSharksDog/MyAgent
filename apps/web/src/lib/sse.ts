/**
 * SSE（Server-Sent Events）解析。
 *
 * ============================================================
 * 一、为什么必须手写解析，而不是用 EventSource
 * ============================================================
 *
 * 浏览器的 `EventSource` 有两个硬限制：
 *   1. **只支持 GET**。而本项目的对话端点是 `POST /api/chat/stream` ——
 *      消息体要放请求体里（不受 URL 长度限制，也不该出现在日志与历史里）。
 *   2. **不能带自定义请求头**（Authorization 之类），也没有 abort 之外的取消能力。
 *
 * 所以只能用 `fetch` 拿到 `ReadableStream<Uint8Array>`，自己按 SSE 规范解帧。
 * 这不是"造轮子"，而是 EventSource 在 POST + 流式场景下根本没有替代品。
 *
 * ============================================================
 * 二、SSE 协议里真正要处理的细节（都是实测会踩到的）
 * ============================================================
 *
 * 1. **换行符有三种**：`\n`、`\r\n`、`\r`。SSE 规范三者等价。
 *    后端用 sse-starlette，它默认发 **`\r\n`**。只按 `'\n\n'` 切帧会一个事件都切不出来。
 *
 * 2. **chunk 边界完全任意**。TCP 不保证"一次 read 正好是一条事件"：
 *    一个事件可能被拆到两次 read 里，也可能三个事件挤在一次 read 里。
 *    所以必须自己维护缓冲区，并且**在缓冲区末尾出现孤立 `\r` 时要等下一个 chunk**
 *    —— 它可能是 `\r\n` 的前半截，提前当换行符处理就会错切。
 *
 * 3. **UTF-8 会被切断**。中文字符是 3 字节，很可能跨 chunk 边界。
 *    必须用 `TextDecoder.decode(chunk, { stream: true })` 持续解码，
 *    否则会出现"每个 chunk 边界一个乱码"。
 *
 * 4. **心跳行必须忽略**。后端设了 `ping=15`，会周期性发送 `: ping` 注释行。
 *    以 `:` 开头的行按规范就是注释——它们不带 data，绝不能当成事件。
 *
 * 5. **data 可以多行**。多行 data 用 `\n` 拼接；且规范规定冒号后有一个前导空格要剥掉
 *    （`data: {"a":1}` 里的那个空格不属于数据）。
 *
 * 6. **流的结束没有"结束帧"**。`reader.read()` 返回 done 就是结束；
 *    如果最后一条事件没有以空行收尾，`flush()` 时也要把它交出来。
 *
 * ============================================================
 * 三、可测试性
 * ============================================================
 *
 * 这个文件刻意**不依赖任何浏览器 API、不做任何网络请求**：
 * `createSseParser()` 是纯函数式的解帧器（吃字符串、吐帧），
 * 网络部分单独放在最底下的 `streamAgentEvents`。
 * 这样 `test/sse.test.mjs` 就能直接在 Node 里用任意切分方式（逐字节、随机、
 * `\r\n` 与 `\n` 混用）验证解帧正确性 —— 不必真的起一个后端。
 */

import type { AgentEvent } from './types'

/** 一帧原始 SSE：已按规范解出字段，但还没做业务解析。 */
export interface SseFrame {
  /** `event:` 字段的值，缺省为空串（此时按 SSE 规范应视作 "message"）。 */
  event: string
  /** `data:` 字段拼接后的值。 */
  data: string
}

export interface SseParser {
  /** 喂入一段文本，返回本次能完整解出的帧（可能为空数组）。 */
  push(chunk: string): SseFrame[]
  /** 流结束时调用：把缓冲区里最后一条没有以空行收尾的事件交出来。 */
  flush(): SseFrame[]
}

/** 剥掉冒号后的**一个**前导空格（SSE 规范规定，不是 trim）。 */
function stripOneLeadingSpace(value: string): string {
  return value.startsWith(' ') ? value.slice(1) : value
}

/**
 * 创建一个有状态的解帧器。
 *
 * 状态只有两个：`buffer`（尚未构成完整行的残留文本）与 `pending`（已解出、
 * 但还没遇到空行的行）。把它们放在闭包里而不是每次重新扫描整个累积串，
 * 是为了让复杂度保持 O(n)：长回答下每 token 都重扫一遍会变成 O(n²)。
 */
export function createSseParser(): SseParser {
  let buffer = ''
  let pending: string[] = []

  /** 把 pending 里的行组装成一帧。返回 null 表示这批行里没有 data（纯注释/心跳）。 */
  function buildFrame(): SseFrame | null {
    let event = ''
    const dataLines: string[] = []

    for (const line of pending) {
      if (line === '') continue
      // `:` 开头是注释：后端的心跳 `: ping` 走的就是这条路径，必须丢弃
      if (line.startsWith(':')) continue

      const colon = line.indexOf(':')
      const field = colon === -1 ? line : line.slice(0, colon)
      const rawValue = colon === -1 ? '' : line.slice(colon + 1)
      const value = stripOneLeadingSpace(rawValue)

      if (field === 'event') event = value
      else if (field === 'data') dataLines.push(value)
      // id / retry 字段本项目用不到，直接忽略（不认识的字段按规范也应忽略）
    }

    pending = []
    if (dataLines.length === 0) return null
    return { event, data: dataLines.join('\n') }
  }

  function push(chunk: string): SseFrame[] {
    buffer += chunk
    const frames: SseFrame[] = []

    let lineStart = 0
    let i = 0

    while (i < buffer.length) {
      const ch = buffer.charCodeAt(i)

      if (ch === 10 /* \n */) {
        pending.push(buffer.slice(lineStart, i))
        i += 1
        lineStart = i
        if (pending.length > 0 && pending[pending.length - 1] === '') {
          const frame = buildFrame()
          if (frame) frames.push(frame)
        }
        continue
      }

      if (ch === 13 /* \r */) {
        // 缓冲区末尾的孤立 \r：可能是被切开的 \r\n，留到下一个 chunk 再判断
        if (i + 1 >= buffer.length) break
        pending.push(buffer.slice(lineStart, i))
        i += buffer.charCodeAt(i + 1) === 10 ? 2 : 1
        lineStart = i
        if (pending[pending.length - 1] === '') {
          const frame = buildFrame()
          if (frame) frames.push(frame)
        }
        continue
      }

      i += 1
    }

    buffer = buffer.slice(lineStart)
    return frames
  }

  function flush(): SseFrame[] {
    // 末尾没有换行符的行同样是一条有效行
    if (buffer.length > 0) {
      pending.push(buffer)
      buffer = ''
    }
    const frame = pending.length > 0 ? buildFrame() : null
    return frame ? [frame] : []
  }

  return { push, flush }
}

/**
 * 把一帧 SSE 解析成 AgentEvent。
 *
 * 规则：
 *   - `event:` 字段优先（后端 `AgentEvent.to_sse()` 会同时写 event 与 data.type）；
 *   - 两者缺失时返回 null（心跳、未知帧）；
 *   - data 不是合法 JSON 时**抛错**而非静默吞掉 —— 沉默的数据丢失是最难查的 bug，
 *     上层会把错误展示给用户。
 */
export function parseAgentEvent(frame: SseFrame): AgentEvent | null {
  if (frame.data === '') return null

  let parsed: unknown
  try {
    parsed = JSON.parse(frame.data)
  } catch {
    throw new Error(`收到无法解析的 SSE 数据（可能是被截断的流）：${frame.data.slice(0, 200)}`)
  }

  if (typeof parsed !== 'object' || parsed === null) return null

  const record = parsed as Record<string, unknown>
  const type = typeof record['type'] === 'string' ? record['type'] : frame.event
  if (!type) return null

  // 这里的断言是必要的：SSE 的 data 是运行时数据，字段是否齐全由后端决定，
  // 前端只能声明成"类型上可选、读取时给默认值"（见 lib/types.ts 的说明）。
  return { ...(parsed as unknown as AgentEvent), type }
}

export interface StreamOptions {
  /** 用于中断（用户点"停止"或组件卸载）。 */
  signal?: AbortSignal
  /** 每解析出一条事件就回调一次。 */
  onEvent?: (event: AgentEvent) => void
}

/**
 * 消费一个 SSE 响应体，逐条产出 Agent 事件。
 *
 * 用异步生成器而不是回调：调用方可以 `for await` 顺序消费，
 * 天然与 React 的"事件 → 状态"归约模型对齐（见 lib/stream.ts）。
 */
export async function* streamAgentEvents(
  response: Response,
  options: StreamOptions = {},
): AsyncGenerator<AgentEvent, void, undefined> {
  const body = response.body
  if (!body) {
    throw new Error('响应没有可读的流（response.body 为 null）')
  }

  const reader = body.getReader()
  const decoder = new TextDecoder('utf-8')
  const parser = createSseParser()

  const emit = (frames: SseFrame[]): AgentEvent[] => {
    const out: AgentEvent[] = []
    for (const frame of frames) {
      const event = parseAgentEvent(frame)
      if (event) {
        options.onEvent?.(event)
        out.push(event)
      }
    }
    return out
  }

  let drained = false
  // signal 也作用于当前挂起的 read；不能只等 fetch 的网络层替我们唤醒它。
  const cancelRead = (): void => { void reader.cancel().catch(() => undefined) }
  options.signal?.addEventListener('abort', cancelRead, { once: true })
  try {
    if (options.signal?.aborted) {
      cancelRead()
      throw new DOMException('已停止生成', 'AbortError')
    }
    for (;;) {
      const { done, value } = await reader.read()
      if (options.signal?.aborted) throw new DOMException('已停止生成', 'AbortError')
      if (done) break
      // stream: true —— 保证跨 chunk 的多字节 UTF-8 字符被正确拼接
      const text = decoder.decode(value, { stream: true })
      for (const event of emit(parser.push(text))) yield event
    }

    // 冲掉解码器与解帧器里可能残留的尾巴
    for (const event of emit(parser.push(decoder.decode()))) yield event
    for (const event of emit(parser.flush())) yield event
    drained = true
  } finally {
    options.signal?.removeEventListener('abort', cancelRead)
    // 只有"没读完就退出"才需要主动取消：调用方 break、抛异常、或被 AbortController 中断。
    // 不取消的话，HTTP 连接会一直挂在服务端（后端会白跑完整个 Agent 循环）。
    if (!drained) {
      await reader.cancel().catch(() => undefined)
    }
    reader.releaseLock()
  }
}
