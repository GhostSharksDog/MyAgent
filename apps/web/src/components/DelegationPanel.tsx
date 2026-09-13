/**
 * 专家派发面板 —— 多 Agent 协作的可视化出口。
 *
 * ============================================================
 * 为什么"谁在处理"必须显示出来
 * ============================================================
 * 多 Agent 系统里，同一句用户提问会由**不同的专家**分别处理，
 * 而它们的答案可能互相矛盾（一个说"突出 AI 项目"，另一个说"补齐分布式经验"）。
 *
 * 如果界面上不显示"这话是谁说的"，用户面对的就是一团来路不明的建议：
 *   - 无法判断该信哪一条（不知道该建议来自哪个专业视角）
 *   - 无法理解为什么最终答案是那样取舍的
 *
 * 所以这里的每张卡片都明确标注专家名与其职责范围，
 * 最终答案则是"协调者的综合判断" —— 层次是清楚的。
 *
 * ============================================================
 * 与工具卡片的区别
 * ============================================================
 * 工具卡片回答的是"Agent 查了什么"（获取信息）；
 * 专家卡片回答的是"谁在思考这个问题"（分配职责）。
 * 两者都是"过程可见"，但一个关于**数据**，一个关于**分工** ——
 * 所以视觉上刻意区分（工具卡片是紧凑的横条，专家卡片是带职责说明的块）。
 */

import { useState } from 'react'
import type { ReactNode } from 'react'

import type { DelegationView } from '../lib/types'
import { IconCheck, IconSpinner, IconX } from './Icons'

function StatusIcon({ status }: { status: DelegationView['status'] }): ReactNode {
  if (status === 'running') {
    return (
      <span className="expert__status" style={{ color: 'var(--accent)' }} title="处理中">
        <IconSpinner size={12} />
      </span>
    )
  }
  if (status === 'failed') {
    return (
      <span className="expert__status" style={{ color: 'var(--danger)' }} title="执行失败">
        <IconX size={13} />
      </span>
    )
  }
  return (
    <span className="expert__status" style={{ color: 'var(--success)' }} title="已完成">
      <IconCheck size={13} />
    </span>
  )
}

export interface DelegationPanelProps {
  delegations: DelegationView[]
  streaming: boolean
}

export function DelegationPanel({ delegations, streaming }: DelegationPanelProps) {
  const [expanded, setExpanded] = useState<number | null>(null)

  if (delegations.length === 0) return null

  const running = delegations.filter((d) => d.status === 'running').length

  return (
    <section className="experts" aria-label="专家协作">
      <header className="experts__head">
        <span className="experts__title">多专家协作</span>
        <span className="experts__count">
          共 {delegations.length} 位专家
          {/* 并发是这一模式的要点，所以把"同时有几位在跑"显式说出来 */}
          {running > 0 ? ` · ${running} 位处理中` : ''}
          {streaming && running === 0 ? ' · 等待汇总' : ''}
        </span>
      </header>

      <div className="experts__list">
        {delegations.map((item, index) => {
          const isOpen = expanded === index
          return (
            <article
              key={`${item.name}-${index}`}
              className="expert"
              data-status={item.status}
            >
              <button
                type="button"
                className="expert__head"
                onClick={() => setExpanded(isOpen ? null : index)}
                aria-expanded={isOpen}
              >
                <StatusIcon status={item.status} />
                <span className="expert__name">{item.name}</span>
                {/* 职责范围：让用户知道"该对这条建议抱多大期望" */}
                <span className="expert__brief" title={item.brief}>
                  {item.brief || '—'}
                </span>
                <span className="expert__toggle">{isOpen ? '收起' : '查看结论'}</span>
              </button>

              {isOpen ? (
                <div className="expert__body">
                  {item.status === 'running' ? (
                    <span className="expert__empty">该专家正在处理…</span>
                  ) : item.output ? (
                    <pre
                      className={
                        item.status === 'failed'
                          ? 'expert__pre expert__pre--error'
                          : 'expert__pre'
                      }
                    >
                      {item.output}
                    </pre>
                  ) : (
                    <span className="expert__empty">（无输出）</span>
                  )}
                </div>
              ) : null}
            </article>
          )
        })}
      </div>

      {/* 参与者与最终答案的关系必须说清楚，否则用户会以为
          上面这些结论就是最终答案。 */}
      <div className="experts__footnote">
        以上为各专家的独立分析；下方「最终答案」是协调者对它们的综合与取舍。
      </div>
    </section>
  )
}
