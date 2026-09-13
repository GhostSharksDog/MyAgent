/**
 * SSE 解帧器自测（Node 内置测试运行器，零依赖）。
 *
 * 为什么要有这个文件：
 * 流式解析的错误都是"时序性"的 —— 一个事件被拆到两次 read、中文被切在
 * 多字节边界上、`\r\n` 与 `\n` 混用、心跳行夹在中间。这些在浏览器里
 * 极难稳定复现，但在这里可以用**任意切分方式**穷举验证。
 *
 * 运行：pnpm test（等价于 node --test test/*.test.mjs）
 * 依赖：Node ≥ 22.18（默认开启 TS 类型剥离，可直接 import .ts 源码）。
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { createSseParser, parseAgentEvent, streamAgentEvents } from '../src/lib/sse.ts'

/** 把一段完整文本按给定长度切成任意块，模拟 TCP 的任意分包。 */
function chunk(text, size) {
  const parts = []
  for (let i = 0; i < text.length; i += size) parts.push(text.slice(i, i + size))
  return parts
}

function parseAll(text, pieceSize) {
  const parser = createSseParser()
  const frames = []
  for (const piece of chunk(text, pieceSize)) frames.push(...parser.push(piece))
  frames.push(...parser.flush())
  return frames
}

const CRLF_STREAM =
  'event: start\r\ndata: {"type":"start","content":"你好"}\r\n\r\n' +
  ': ping\r\n\r\n' +
  'event: token\r\ndata: {"type":"token","step":1,"content":"简历"}\r\n\r\n' +
  'event: done\r\ndata: {"type":"done","steps_used":1}\r\n'

test('解出 sse-starlette 风格的 \\r\\n 分帧，并忽略 ping 心跳', () => {
  const frames = parseAll(CRLF_STREAM, CRLF_STREAM.length)
  assert.equal(frames.length, 3)
  assert.deepEqual(
    frames.map((f) => f.event),
    ['start', 'token', 'done'],
  )
  assert.equal(JSON.parse(frames[0].data).content, '你好')
})

test('任意分包（含逐字节）都得到完全相同的结果', () => {
  const expected = parseAll(CRLF_STREAM, CRLF_STREAM.length)
  for (let size = 1; size <= 17; size += 1) {
    const actual = parseAll(CRLF_STREAM, size)
    assert.deepEqual(actual, expected, `按 ${size} 字符切分时结果不一致`)
  }
})

test('\\n 与 \\r\\n 混用都能正确分帧', () => {
  const text = 'event: a\ndata: {}\n\nevent: b\r\ndata: {}\r\n\r\n'
  const frames = parseAll(text, 1)
  assert.deepEqual(
    frames.map((f) => f.event),
    ['a', 'b'],
  )
})

test('孤立的 \\r 被切开时不会被误判为换行', () => {
  const parser = createSseParser()
  const frames = [
    ...parser.push('event: x\r'),
    ...parser.push('\ndata: {"type":"x"}\r\n\r\n'),
  ]
  assert.equal(frames.length, 1)
  assert.equal(frames[0].event, 'x')
  assert.equal(frames[0].data, '{"type":"x"}')
})

test('data 多行按规范用 \\n 拼接，且剥离冒号后的一个空格', () => {
  const frames = parseAll('data: {"a":1,\ndata: "b":2}\n\n', 3)
  assert.deepEqual(JSON.parse(frames[0].data), { a: 1, b: 2 })
})

test('没有尾部空行的事件在 flush 时也要交出来', () => {
  const parser = createSseParser()
  const pushed = parser.push('event: tail\ndata: {"type":"tail"}')
  assert.equal(pushed.length, 0, '未遇到空行时不应提前产出')
  const flushed = parser.flush()
  assert.equal(flushed.length, 1)
  assert.equal(flushed[0].event, 'tail')
})

test('纯注释帧不产出任何事件', () => {
  const frames = parseAll(': ping\n\n: keep-alive\n\n', 4)
  assert.deepEqual(frames, [])
})

test('event 字段缺省时以 data.type 为准', () => {
  const frames = parseAll('data: {"type":"token"}\n\n', 5)
  const event = parseAgentEvent(frames[0])
  assert.equal(event.type, 'token')
})

test('data 不是合法 JSON 时抛错而不是静默丢弃', () => {
  const frames = parseAll('event: token\ndata: {"type":"tok\n\n', 2)
  assert.throws(() => parseAgentEvent(frames[0]), /无法解析/)
})

test('streamAgentEvents 能跨 UTF-8 字节边界正确解码（走真实 ReadableStream）', async () => {
  const text =
    'event: token\r\ndata: {"type":"token","step":1,"content":"多字节中文🙂"}\r\n\r\n' +
    'event: done\r\ndata: {"type":"done","steps_used":1,"stopped_reason":"finished"}\r\n\r\n'

  const bytes = new TextEncoder().encode(text)
  // 故意按 3 字节切：中文字符正好是 3 字节，必然把字符劈开
  const stream = new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += 3) {
        controller.enqueue(bytes.slice(i, i + 3))
      }
      controller.close()
    },
  })

  const events = []
  for await (const event of streamAgentEvents(new Response(stream))) {
    events.push(event)
  }

  assert.equal(events.length, 2)
  assert.equal(events[0].type, 'token')
  assert.equal(events[0].content, '多字节中文🙂')
  assert.equal(events[1].type, 'done')
  assert.equal(events[1].stopped_reason, 'finished')
})

test('消费方提前退出时会取消连接（否则后端会白跑完整轮）', async () => {
  let cancelled = false
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(
        new TextEncoder().encode('event: start\ndata: {"type":"start"}\n\n'),
      )
      // 不 close：模拟还在持续输出的流
    },
    cancel() {
      cancelled = true
    },
  })

  for await (const event of streamAgentEvents(new Response(stream))) {
    assert.equal(event.type, 'start')
    break // 只读一条就退出
  }

  assert.equal(cancelled, true, '提前退出后应当 cancel 掉 reader')
})
