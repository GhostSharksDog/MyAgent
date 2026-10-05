/**
 * 空状态：首屏的第一印象。
 *
 * 对一个作品集项目来说这一屏很重要 —— 招聘方通常只会花 30 秒。
 * 所以这里不放"请输入你的问题"这种废话，而是：
 *   1. 一句话说明这是什么（手写内核的 ReAct Agent，不是套壳聊天）；
 *   2. 用徽标把**真实的后端状态**摆出来（模型名、工具数、步数上限、会话后端），
 *      这些数据都来自 /healthz 与 /api/meta，不是写死的文案；
 *   3. 给四个能立刻体现"工具调用"的示例问题 —— 点击即填入输入框，回车即发。
 *
 * 示例问题刻意都指向需要外部信息才能回答的场景（简历、岗位、匹配度），
 * 因为只有这样模型才会真的去调工具、才能看到工具卡片和时间线。
 */

import type { ApiMeta, HealthStatus } from '../lib/types'

const PROMPTS: { tag: string; text: string }[] = [
  { tag: 'resume-match', text: '把简历和这个 JD 做一次结构化匹配打分，并给出差距清单' },
  { tag: 'rewrite', text: '帮我按后端/Agent 方向改写简历里最有价值的三条经历' },
  { tag: 'interview', text: '基于我的简历模拟一场技术面试，先问三个由浅入深的问题' },
  { tag: 'rag', text: '检索岗位知识库，找出最适合我的三个岗位并说明理由' },
]

export interface EmptyStateProps {
  meta: ApiMeta | null
  health: HealthStatus | null
  onPick: (text: string) => void
}

export function EmptyState({ meta, health, onPick }: EmptyStateProps) {
  const model = meta?.model ?? health?.model ?? '—'
  const toolCount = meta?.tool_count ?? health?.tools.length ?? 0
  const maxSteps = meta?.max_steps
  const backend = meta?.session_backend ?? health?.session_backend ?? '—'

  return (
    <div className="empty">
      <div className="empty__inner">
        <div className="empty__brand">
          <span className="empty__mark">JP</span>
          <div>
            <div className="empty__title">Legacy · Agent 控制台</div>
            <div className="empty__subtitle">
              手写 ReAct 内核的求职 Agent。每一次回答背后的推理步骤与工具调用都会实时展开在这里。
            </div>
          </div>
        </div>

        <div className="empty__facts">
          <span className="pill">
            <span className="pill__label">model</span>
            <span className="pill__value">{model}</span>
          </span>
          <span className="pill">
            <span className="pill__label">tools</span>
            <span className="pill__value">{toolCount}</span>
          </span>
          {typeof maxSteps === 'number' ? (
            <span className="pill">
              <span className="pill__label">max_steps</span>
              <span className="pill__value">{maxSteps}</span>
            </span>
          ) : null}
          <span className="pill">
            <span className="pill__label">sessions</span>
            <span className="pill__value">{backend}</span>
          </span>
        </div>

        <div className="empty__prompts">
          {PROMPTS.map((prompt) => (
            <button
              key={prompt.tag}
              type="button"
              className="prompt-chip"
              onClick={() => onPick(prompt.text)}
            >
              <span className="prompt-chip__tag">{prompt.tag}</span>
              {prompt.text}
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
