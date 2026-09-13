#!/usr/bin/env node
/**
 * 前后端契约集成验证。
 *
 * 【为什么需要这个脚本 —— 它补的是一个真实的验证缺口】
 *
 * 前端有 43 个单元测试，但它们全部跑在**录制的假事件**上；
 * 后端有 439 个测试，但它们只验证自己的序列化，不知道前端怎么读。
 * 也就是说：**"后端的 SSE 输出"与"前端的 SSE 解析"之间没有任何自动化检查**。
 *
 * 而这个接缝恰恰最容易出问题 —— 字段改名、事件类型新增、
 * 序列化遗漏字段，任何一处不一致都会让前端静默地少渲染一块内容
 * （前端刻意忽略未知事件类型，所以不会崩，只是"看起来没反应"）。
 *
 * 本脚本直接复用前端的真实解析与归约代码去消费后端的真实响应，
 * 因此它是唯一能发现"两边各自都对、合起来不对"这类问题的手段。
 *
 * 用法（后端需先启动：..\..\scripts\dev.ps1 serve）：
 *     node scripts/verify-backend.mjs
 *     node scripts/verify-backend.mjs "自定义问题"
 *
 * 退出码：0 表示全部通过，1 表示有检查失败。
 */

import { applyEvent, computeTurnView, emptyTurn } from '../src/lib/stream.ts'
import { streamAgentEvents } from '../src/lib/sse.ts'

const BASE = process.env.JOBPILOT_BACKEND ?? 'http://127.0.0.1:8000'
const QUESTION = process.argv[2] ?? '我的简历里有没有消息队列相关的经验？请引用出处。'

let failures = 0

function check(label, ok, detail = '') {
  const mark = ok ? '  ✓' : '  ✗'
  console.log(`${mark} ${label}${detail ? `  ${detail}` : ''}`)
  if (!ok) failures += 1
}

function section(title) {
  console.log(`\n${'─'.repeat(64)}\n${title}\n${'─'.repeat(64)}`)
}

async function main() {
  console.log(`目标后端：${BASE}`)

  // ---------- 1. 健康检查 ----------
  section('1. 后端可用性与能力')
  let health
  try {
    health = await (await fetch(`${BASE}/healthz`)).json()
  } catch (err) {
    console.error(`\n无法连接后端：${err.message}`)
    console.error('请先启动： .\\scripts\\dev.ps1 serve')
    process.exit(1)
  }
  check('健康检查返回 ok', health.status === 'ok', `model=${health.model}`)
  check('已注册工具', Array.isArray(health.tools) && health.tools.length > 0, health.tools.join(', '))
  check('暴露会话后端', Boolean(health.session_backend), `backend=${health.session_backend}`)

  // ---------- 2. 创建会话 ----------
  section('2. 会话创建')
  const session = await (await fetch(`${BASE}/api/sessions`, { method: 'POST' })).json()
  check('会话已创建', Boolean(session.id), `id=${session.id.slice(0, 8)}…`)

  // ---------- 3. 真实流式对话，用前端代码消费 ----------
  section('3. 流式对话（前端解析器消费真实响应）')
  console.log(`\n提问：${QUESTION}\n`)

  const response = await fetch(`${BASE}/api/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: QUESTION, session_id: session.id }),
  })

  check('HTTP 200', response.status === 200, `status=${response.status}`)
  check(
    'Content-Type 是 text/event-stream',
    (response.headers.get('content-type') ?? '').includes('text/event-stream'),
    response.headers.get('content-type') ?? '',
  )

  // 【关键】这里用的是前端真实的解析器与归约器，不是脚本里另写一份。
  // 也只有这样，验证的才是"前端能不能读懂后端"，而不是"我这个脚本能不能"。
  let turn = emptyTurn('connecting')
  const eventTypes = []

  for await (const event of streamAgentEvents(response)) {
    eventTypes.push(event.type)
    turn = applyEvent(turn, event)
  }

  const view = computeTurnView(turn)

  // 工具调用的配对（tool_call ↔ tool_result）由归约器负责，
  // 这里只是把它的结果读出来核对 —— 如果脚本自己配一遍，就测不到归约器了。
  const toolCards = turn.steps.flatMap((s) => s.toolCalls)

  check('收到 start 事件', eventTypes[0] === 'start', eventTypes[0])
  check('收到 done 事件', eventTypes.at(-1) === 'done', eventTypes.at(-1))
  check('收到 final 事件', eventTypes.includes('final'))
  check('解析器认得所有事件类型', !eventTypes.includes(undefined))

  // 事件序列本身是契约的一部分：顺序错了前端状态机会乱。
  // 只打印压缩后的形态 —— token 事件动辄上百条，全打出来没法看。
  const compressed = []
  for (const type of eventTypes) {
    if (type === 'token' && compressed.at(-1)?.type === 'token') {
      compressed.at(-1).n += 1
    } else {
      compressed.push({ type, n: 1 })
    }
  }
  console.log(
    `\n  事件序列：${compressed
      .map((e) => (e.n > 1 ? `${e.type}×${e.n}` : e.type))
      .join(' → ')}`,
  )

  // ---------- 4. 工具调用可视化所需的数据是否齐全 ----------
  section('4. 工具调用卡片的数据完整性')
  if (toolCards.length === 0) {
    console.log('  （本轮没有调用工具）')
    check('无工具调用时也能正常完成', turn.phase === 'done')
  } else {
    for (const card of toolCards) {
      const complete =
        card.name &&
        card.args !== null &&
        typeof card.args === 'object' &&
        card.status !== 'running' &&
        typeof card.output === 'string'
      console.log(
        `  ${complete ? '✓' : '✗'} ${card.name}  args=${Object.keys(card.args ?? {}).length} 项  ` +
          `status=${card.status}  ${card.durationMs ?? '-'}ms  ` +
          `truncated=${card.truncated}  output=${card.output?.length ?? 0} 字`,
      )
      if (!complete) failures += 1
    }
    check(
      '每个工具调用都有参数、终态与输出',
      toolCards.every((c) => c.status !== 'running' && c.output !== undefined),
      `共 ${toolCards.length} 次调用`,
    )
    check(
      '工具调用被正确配对（无残留 running）',
      toolCards.every((c) => c.status === 'ok' || c.status === 'failed'),
    )
  }

  // ---------- 5. 归约后的 UI 状态 ----------
  section('5. 前端归约后的状态')
  check('阶段为 done', turn.phase === 'done', `phase=${turn.phase}`)
  check('有最终答案文本', turn.answer.length > 0, `${turn.answer.length} 字`)
  check('答案已定稿', view.answerIsFinal === true)
  check('用量已解析', Boolean(turn.usage), turn.usage ? JSON.stringify(turn.usage) : '缺失')
  check(
    '停止原因是合法枚举',
    ['finished', 'max_steps', 'loop_detected', 'error'].includes(turn.stoppedReason),
    turn.stoppedReason,
  )
  check('步数已记录', turn.stepsUsed > 0, `${turn.stepsUsed} 步`)
  check(
    '最终答案那一步已从时间线排除（避免重复展示）',
    !view.timeline.some((s) => s.index === turn.finalStep),
  )

  console.log('\n  ── 最终答案（前 300 字）──')
  console.log(`  ${(turn.answer ?? '').replace(/\n/g, '\n  ').slice(0, 300)}`)
  console.log(`\n  ── 思考时间线（${view.timeline.length} 步）──`)
  for (const item of view.timeline) {
    console.log(`  步骤 ${item.index}：${(item.text ?? '').slice(0, 70)}`)
    for (const tc of item.toolCalls) {
      const icon = tc.status === 'ok' ? '✓' : tc.status === 'failed' ? '✗' : '⏳'
      console.log(`    ⚙ ${tc.name} → ${icon} ${tc.status} ${tc.durationMs ?? '-'}ms`)
    }
  }

  // ---------- 6. 会话持久化 ----------
  section('6. 会话持久化')
  const detail = await (await fetch(`${BASE}/api/sessions/${session.id}`)).json()
  check('会话已记录轮次', detail.turns?.length === 2, `${detail.turns?.length ?? 0} 条消息`)
  check('会话标题由首句生成', Boolean(detail.title) && detail.title !== '（未命名会话）',
    `title=${detail.title}`)
  check('累计用量已记录', detail.total_tokens > 0, `${detail.total_tokens} tokens`)

  const list = await (await fetch(`${BASE}/api/sessions`)).json()
  check('会话出现在列表中', list.sessions.some((s) => s.id === session.id))

  // ---------- 7. 清理 ----------
  await fetch(`${BASE}/api/sessions/${session.id}`, { method: 'DELETE' })

  // ---------- 8. 三种 Agent 形态 ----------
  //
  // 【为什么这一段不能省】
  // 规划型与多 Agent 在实现完成后**一度无法从 API 触发** —— 它们只存在于
  // Python 模块里，HTTP 入口永远走默认的 ReAct。前端于是永远看不到
  // plan / delegate 事件，那些面板也就永远不显示。
  // 这是"功能实现了但没有被接上"的典型：单元测试全绿，端到端却走不到。
  await checkModes()

  section('结果')
  if (failures === 0) {
    console.log('✓ 全部检查通过：前后端契约一致')
    process.exit(0)
  }
  console.log(`✗ ${failures} 项检查失败`)
  process.exit(1)
}

/** 每种形态的"特征事件"——它必须出现，否则说明该形态没被真正走到。 */
const MODE_SIGNATURES = {
  react: [],
  plan: ['plan', 'plan_step'],
  multi: ['delegate', 'delegate_result'],
}

async function checkModes() {
  section('8. 三种 Agent 形态（同一请求体，只改 mode）')

  const meta = await (await fetch(`${BASE}/api/meta`)).json()
  check('后端声明了可用形态', Array.isArray(meta.agent_modes), (meta.agent_modes ?? []).join(', '))

  const question = '我的简历里有没有大数据相关的经验？请一句话回答。'

  for (const [mode, signatures] of Object.entries(MODE_SIGNATURES)) {
    const response = await fetch(`${BASE}/api/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question, mode }),
    })

    if (response.status !== 200) {
      check(`${mode} 形态返回 200`, false, `status=${response.status}`)
      continue
    }

    // 同样复用前端的真实解析器与归约器
    let turn = emptyTurn('connecting')
    const types = []
    for await (const event of streamAgentEvents(response)) {
      types.push(event.type)
      turn = applyEvent(turn, event)
    }

    const missing = signatures.filter((s) => !types.includes(s))
    check(
      `${mode} 形态产出特征事件`,
      missing.length === 0,
      missing.length ? `缺少 ${missing.join(', ')}` : signatures.join(' + ') || '（默认形态，无特有事件）',
    )
    check(
      `${mode} 形态正常结束`,
      types.at(-1) === 'done',
      `phase=${turn.phase} reason=${turn.stoppedReason} code=${types.at(-1)}`,
    )

    // 形态特有的归约结果 —— 这才是"前端真的能渲染它"的证据
    if (mode === 'plan') {
      const steps = turn.plan?.steps ?? []
      check('计划快照被前端正确归约', steps.length > 0, `${steps.length} 步`)
      check(
        '计划步骤都到达终态',
        steps.length > 0 && steps.every((s) => ['done', 'failed', 'skipped'].includes(s.status)),
        steps.map((s) => s.status).join(','),
      )
    }
    if (mode === 'multi') {
      check(
        '专家派发被前端正确配对',
        turn.delegations.length > 0 && turn.delegations.every((d) => d.status !== 'running'),
        turn.delegations.map((d) => `${d.name}:${d.status}`).join(' '),
      )
    }
  }
}

main().catch((err) => {
  console.error('\n脚本异常：', err)
  process.exit(1)
})
